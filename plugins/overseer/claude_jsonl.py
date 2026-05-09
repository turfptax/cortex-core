"""Parser for Claude Code .jsonl session files.

Claude Code stores each interactive session as a JSON-Lines file at
~/.claude/projects/<project>/<session-uuid>.jsonl — one JSON object
per line. Object types include "queue-operation" (input history),
"user", "assistant", "summary", "system", "tool_use", "tool_result".

This module:
  - parses metadata from the file (session id, time range, message
    counts, cwd, git branch, version)
  - yields a chronological message list of role + plaintext + timestamp
    suitable for feeding into the overseer's session-summarization prompt
  - computes a sha256 file hash for dedup

Claude Desktop conversations on Windows live inside Electron's IndexedDB
at %APPDATA%/Claude/IndexedDB/, which requires a different parser
(LevelDB-backed binary store). Not handled here — slice 3d ships Claude
Code support; Desktop follows.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path


log = logging.getLogger("plugin.overseer.claude_jsonl")


CLAUDE_CODE_SOURCE = "claude-code"


# ── File hashing ────────────────────────────────────────────────

def file_sha256(path) -> str:
    """SHA256 of a file's content for dedup."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ── Content extraction ──────────────────────────────────────────

def extract_content_text(content) -> str:
    """Reduce Claude content (str | list[block]) to plain text.

    Anthropic message content can be a string or a list of typed blocks
    (text, tool_use, tool_result, image). We extract human-readable text
    and represent tool calls as `[tool_use: NAME]` markers so the
    summarizer can see the *shape* of the work without the full payload.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", "?")
                parts.append("[tool_use: {}]".format(name))
            elif btype == "tool_result":
                # tool_result.content can itself be a string or list of blocks
                inner = block.get("content")
                if isinstance(inner, str):
                    snippet = inner[:200].replace("\n", " ")
                    parts.append("[tool_result: {}]".format(snippet))
                else:
                    parts.append("[tool_result]")
            elif btype == "image":
                parts.append("[image]")
            else:
                parts.append("[{}]".format(btype or "block"))
        return "\n".join(p for p in parts if p)
    # Anything else: best-effort stringify
    return str(content)[:1000]


def _has_tool_use(content) -> bool:
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in content
        )
    return False


# ── Extended-stats extractor (Slice 4 CP1a) ─────────────────────
#
# Walks the file once, gathers token usage, models, and file-path
# touches from tool_use blocks. Skips message reconstruction —
# returns only aggregates suitable for the project_summaries
# rollup. Cheap to run as a backfill over many files.

# Path fragments we never count when ranking "files touched per
# project". Configurable later (project_summary_settings table)
# but a basic list ships with CP1a.
EXCLUDED_PATH_FRAGMENTS = (
    "/.git/",
    "/node_modules/",
    "/__pycache__/",
    "/dist/",
    "/build/",
    "/.venv/",
    "/venv/",
    "/.next/",
    "/.cache/",
    "/.idea/",
    "/.vscode/",
    "/target/",
    "/.pytest_cache/",
    "/.mypy_cache/",
    "/.ruff_cache/",
)


def _path_excluded(path: str) -> bool:
    """True if the path looks like generated/dependency content
    we don't want to count toward 'files touched'."""
    if not path:
        return True
    # Normalize separators so Windows paths are excluded too.
    normalized = "/" + path.replace("\\", "/").lstrip("/")
    for frag in EXCLUDED_PATH_FRAGMENTS:
        if frag in normalized:
            return True
    return False


# Tool inputs that name a file path. The Anthropic tool-use schema
# carries these under different keys depending on the tool.
_FILE_PATH_TOOL_INPUTS = ("file_path", "path", "notebook_path")


def _extract_tool_paths(content) -> list[str]:
    """Pull file paths out of tool_use blocks in an assistant
    message's content list. Returns one entry per tool_use that
    names a file path (post-exclusion filtering happens upstream so
    counts here are raw)."""
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        inp = block.get("input")
        if not isinstance(inp, dict):
            continue
        for k in _FILE_PATH_TOOL_INPUTS:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
                break  # one path per tool_use
    return out


# For active_minutes calculation: gaps between consecutive
# user/assistant messages BELOW this threshold count as "active
# work time"; gaps above it are treated as the user walked away
# (different work session, even if same .jsonl file kept open).
ACTIVE_GAP_THRESHOLD_MINUTES = 30


def extract_extended_stats(path) -> dict:
    """Stream a Claude Code .jsonl and return the extended stats
    block needed for project_summaries rollup. Cheap — no message
    reconstruction.

    Returns dict with:
      tokens_input_total          int  (sum across all assistant turns)
      tokens_output_total         int
      tokens_cache_creation_total int
      tokens_cache_read_total     int
      models_used                 dict {model: count}  (model is the
                                  exact .model string from the message)
      file_paths                  dict {path: hit_count}
                                  (post-exclusion; only paths we'd
                                  count toward project-files ranking)
      file_paths_excluded         dict {path: hit_count}
                                  (kept separately so audit + tuning
                                  of the exclusion list is easy)
      tool_use_paths_total        int  (raw count of tool_use blocks
                                  with a file-path input — equal to
                                  sum of file_paths and file_paths_
                                  excluded combined)
      first_assistant_at          str  (ISO timestamp of first
                                  assistant message, or None)
      last_assistant_at           str
      active_minutes              int  (sum of inter-message gaps
                                  under ACTIVE_GAP_THRESHOLD_MINUTES;
                                  the "real time spent on this
                                  session" — drops the multi-hour
                                  walk-away gaps that wall-clock
                                  duration_minutes includes)
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            "Claude Code .jsonl not found: {}".format(p))

    tokens_in = 0
    tokens_out = 0
    tokens_cc = 0
    tokens_cr = 0
    models: dict = {}
    files: dict = {}
    files_excl: dict = {}
    tool_use_paths_total = 0
    first_asst = None
    last_asst = None

    # Active-minutes accumulator — collect ALL user+assistant
    # timestamps (not just assistant) so the gap series reflects
    # real interaction cadence. Computed after the walk.
    interaction_timestamps: list[str] = []

    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type")

            # Collect interaction timestamps for active-minutes from
            # both user and assistant turns (system/tool_result/
            # queue entries don't count as user activity).
            if t in ("user", "assistant"):
                ts = obj.get("timestamp")
                if ts:
                    interaction_timestamps.append(ts)

            if t != "assistant":
                continue

            msg = obj.get("message") or {}
            ts = obj.get("timestamp")

            # Token usage. Anthropic's usage block omits keys when
            # zero — default each to 0.
            usage = msg.get("usage") or {}
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
            tokens_cc += int(usage.get("cache_creation_input_tokens") or 0)
            tokens_cr += int(usage.get("cache_read_input_tokens") or 0)

            # Model — different assistant messages within one session
            # can use different models (e.g. main task on opus, tool
            # rewrite on haiku). Track each.
            model = msg.get("model")
            if isinstance(model, str) and model:
                models[model] = models.get(model, 0) + 1

            # File paths from tool_use blocks
            paths = _extract_tool_paths(msg.get("content"))
            for pth in paths:
                tool_use_paths_total += 1
                bucket = files_excl if _path_excluded(pth) else files
                bucket[pth] = bucket.get(pth, 0) + 1

            if ts:
                if first_asst is None or ts < first_asst:
                    first_asst = ts
                if last_asst is None or ts > last_asst:
                    last_asst = ts

    active_minutes = _compute_active_minutes(interaction_timestamps)

    return {
        "tokens_input_total": tokens_in,
        "tokens_output_total": tokens_out,
        "tokens_cache_creation_total": tokens_cc,
        "tokens_cache_read_total": tokens_cr,
        "models_used": models,
        "file_paths": files,
        "file_paths_excluded": files_excl,
        "tool_use_paths_total": tool_use_paths_total,
        "first_assistant_at": first_asst,
        "last_assistant_at": last_asst,
        "active_minutes": active_minutes,
    }


def _compute_active_minutes(timestamps: list[str]) -> int:
    """Sum of inter-message gaps under ACTIVE_GAP_THRESHOLD_MINUTES.

    Walks the (possibly out-of-order) timestamp list, sorts, then
    accumulates each pair-gap that falls under the threshold. Gaps
    above the threshold are treated as a "walk-away" boundary — the
    user came back later, that interval doesn't count as active work
    time even though wall-clock duration_minutes includes it.

    Returns 0 if fewer than 2 timestamps. Returns whole minutes
    rounded down (we don't want half-minute precision in a long
    aggregate — too easy to look spuriously precise).
    """
    if len(timestamps) < 2:
        return 0
    parsed: list[datetime] = []
    for ts in timestamps:
        try:
            parsed.append(_parse_iso(ts))
        except (ValueError, AttributeError):
            continue
    if len(parsed) < 2:
        return 0
    parsed.sort()
    threshold_s = ACTIVE_GAP_THRESHOLD_MINUTES * 60
    total_s = 0.0
    for i in range(1, len(parsed)):
        gap_s = (parsed[i] - parsed[i - 1]).total_seconds()
        if 0 < gap_s <= threshold_s:
            total_s += gap_s
    return int(total_s // 60)


# ── Main parser ─────────────────────────────────────────────────

def parse_claude_code_jsonl(path) -> tuple[dict, list[dict]]:
    """Parse a Claude Code session file.

    Returns:
        (metadata, messages)

        metadata: dict with session_id, started_at, ended_at,
                  duration_minutes, message_count, user_message_count,
                  assistant_message_count, tool_use_count, cwd,
                  git_branch, version, parse_errors, total_lines.
        messages: list of {role, content_text, timestamp, has_tool_use,
                  is_sidechain} in chronological order.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError("Claude Code .jsonl not found: {}".format(p))

    metadata: dict = {
        "session_id": None,
        "started_at": None,
        "ended_at": None,
        "duration_minutes": 0,
        "message_count": 0,
        "user_message_count": 0,
        "assistant_message_count": 0,
        "tool_use_count": 0,
        "cwd": None,
        "git_branch": None,
        "version": None,
        "entrypoint": None,
        "parse_errors": 0,
        "total_lines": 0,
    }
    messages: list[dict] = []

    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            metadata["total_lines"] += 1
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                metadata["parse_errors"] += 1
                continue

            t = obj.get("type")
            ts = obj.get("timestamp")
            sess = obj.get("sessionId")

            if sess and not metadata["session_id"]:
                metadata["session_id"] = sess

            if ts:
                if (metadata["started_at"] is None
                        or ts < metadata["started_at"]):
                    metadata["started_at"] = ts
                if (metadata["ended_at"] is None
                        or ts > metadata["ended_at"]):
                    metadata["ended_at"] = ts

            # Capture environment fields from the first object that has them
            for key, src in (("cwd", "cwd"),
                             ("git_branch", "gitBranch"),
                             ("version", "version"),
                             ("entrypoint", "entrypoint")):
                if not metadata[key] and obj.get(src):
                    metadata[key] = obj.get(src)

            if t in ("user", "assistant"):
                msg = obj.get("message") or {}
                role = msg.get("role") or t
                content = msg.get("content")
                content_text = extract_content_text(content)
                tool_use = _has_tool_use(content)

                metadata["message_count"] += 1
                if role == "user":
                    metadata["user_message_count"] += 1
                elif role == "assistant":
                    metadata["assistant_message_count"] += 1
                if tool_use:
                    metadata["tool_use_count"] += 1

                messages.append({
                    "role": role,
                    "content_text": content_text,
                    "timestamp": ts,
                    "has_tool_use": tool_use,
                    "is_sidechain": bool(obj.get("isSidechain", False)),
                })

    # Compute duration in whole minutes
    if metadata["started_at"] and metadata["ended_at"]:
        try:
            d1 = _parse_iso(metadata["started_at"])
            d2 = _parse_iso(metadata["ended_at"])
            metadata["duration_minutes"] = max(
                0, int((d2 - d1).total_seconds() / 60))
        except Exception as e:
            log.warning("could not compute duration for %s: %s", p, e)

    return metadata, messages


def _parse_iso(s: str) -> datetime:
    """Parse ISO 8601 — tolerates trailing 'Z'."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ── Transcript builder for summarization prompts ────────────────

def build_transcript_for_summary(messages: list[dict],
                                 *, max_chars: int = 30000,
                                 head_n: int = 30,
                                 tail_n: int = 20) -> tuple[str, dict]:
    """Pack the message list into a transcript suitable for an LLM
    summarization prompt.

    Strategy:
      - Skip sidechain messages (Claude Code branching exploration)
      - If the trimmed transcript fits under max_chars, return all of it.
      - Otherwise: head_n + tail_n with a `[... K omitted ...]` marker.

    Returns (transcript_text, stats) where stats has the numbers used.
    """
    trimmed = [m for m in messages if not m.get("is_sidechain")]

    def fmt(m):
        role = (m.get("role") or "?").upper()[:1]
        ts = (m.get("timestamp") or "")[:19]
        content = (m.get("content_text") or "").strip()
        # Trim each individual message to keep one runaway message from
        # eating the whole window.
        if len(content) > 4000:
            content = content[:4000] + " […]"
        return "[{} {}] {}".format(ts, role, content)

    full = "\n\n".join(fmt(m) for m in trimmed)
    if len(full) <= max_chars:
        return full, {
            "messages_total": len(messages),
            "messages_used": len(trimmed),
            "messages_omitted": 0,
            "strategy": "full",
            "char_length": len(full),
        }

    # Head + tail
    head = trimmed[:head_n]
    tail = trimmed[-tail_n:] if tail_n > 0 else []
    omitted = len(trimmed) - len(head) - len(tail)
    if omitted < 0:
        # Edge case: head + tail overlap. Use only head.
        head = trimmed[:head_n + tail_n]
        tail = []
        omitted = 0

    parts = [fmt(m) for m in head]
    if omitted > 0:
        parts.append("[ ... {} messages omitted ... ]".format(omitted))
    parts.extend(fmt(m) for m in tail)
    text = "\n\n".join(parts)

    # Final sanity trim if still too big
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... transcript hard-truncated ...]"

    return text, {
        "messages_total": len(messages),
        "messages_used": len(head) + len(tail),
        "messages_omitted": omitted,
        "strategy": "head-tail",
        "head_n": len(head),
        "tail_n": len(tail),
        "char_length": len(text),
    }


# ── Path helpers ────────────────────────────────────────────────

def claude_code_session_id_from_path(path) -> str:
    """Filename minus .jsonl suffix is the session UUID for Claude Code."""
    return Path(path).stem


def claude_code_imported_id(session_id: str) -> str:
    """Stable composite ID for an imported Claude Code session."""
    return "{}:{}".format(CLAUDE_CODE_SOURCE, session_id)


def canonicalize_project_name(raw: str) -> str:
    """Normalize a project name to a stable, human-friendly tag.

    The Claude Code .jsonl files carry the user's `cwd` as a full
    path (e.g. `C:\\dev\\ClientA\\ClientA-recruitment`). We tag everything
    by basename (`ClientA-recruitment`) so things group across machines
    and across moves.

    This helper is the canonical implementation; both the import path
    AND any backfill migration MUST go through it so `imported_sessions
    .project` and `tags.tag = "project:<name>"` stay in lockstep.

    Behavior:
      - Empty / None → ""
      - Path-shaped (contains `/` or `\\` or starts with drive letter
        like `C:`) → basename of the path
      - Already-clean → returned unchanged (whitespace stripped)
      - Trailing/leading whitespace stripped; internal whitespace
        preserved (so "<Project> Finance - General" stays one project)
      - Trailing slashes/backslashes stripped before basename extraction
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # If it looks path-shaped, take the basename. We treat backslash and
    # forward slash as separators (the .jsonl came from the user's
    # machine, not ours; we get Windows paths on a Linux Pi).
    if "/" in s or "\\" in s or (len(s) >= 2 and s[1] == ":"):
        # Strip trailing separators THEN split. Handles "C:\\foo\\bar\\"
        # and "/foo/bar/" alike.
        clean = s.replace("\\", "/").rstrip("/")
        parts = clean.split("/")
        if parts:
            s = parts[-1]
    return s.strip()
