"""Parser for Anthropic Data Export ZIP — the Claude Desktop / claude.ai
conversation archive.

Background
----------
Slice 3j tried to read Claude Desktop's IndexedDB directly and hit a
dead end (Electron-LevelDB binary store, schema versioned by the
desktop client). The note from that closure (memory/overseer_3j_closed_
not_needed.md) directs us at the Anthropic Data Export ZIP instead —
users request their data from the Anthropic account settings, receive
a ZIP a few hours later, and the ZIP carries the full conversation
history in JSON.

Phase 1 (2026-05-27) scope
--------------------------
This module is a SCAFFOLD — it lands the parser + a dry-run endpoint
that reports counts + a sample conversation. It does NOT yet write
into `imported_sessions`. Full ingest (mirror what the Claude Code
import does: hash dedup, per-message storage, gist queueing) is a
follow-up slice once the parse shape is validated against a real
export.

The export ZIP shape (as observed in exports up to mid-2026)
-------------------------------------------------------------
The ZIP contains a small set of JSON files at the root:

  conversations.json     list of conversation objects
  projects.json          list of project objects (newer exports)
  users.json             account metadata

Each `conversations.json` entry looks like:

  {
    "uuid": "abc123...",
    "name": "Some conversation title",
    "created_at": "2025-12-15T13:42:11.000000Z",
    "updated_at": "2025-12-15T15:08:33.000000Z",
    "account": {"uuid": "..."},
    "chat_messages": [
      {
        "uuid": "msg-uuid",
        "text": "...full message body...",
        "sender": "human" | "assistant",
        "created_at": "...",
        "updated_at": "...",
        "attachments": [...],
        "files": [...],
        "content": [   # newer exports — block-shaped, like API responses
          {"type": "text", "text": "..."}
        ]
      },
      ...
    ]
  }

Older exports use `text` as a flat string. Newer exports use a typed
`content` block list (closer to the Anthropic Messages API shape).
We tolerate both.

Importer notes
--------------
This source is `claude-desktop`. The session_id we synthesize is the
conversation `uuid` prefixed with that source label so it can't
collide with `claude-code` session ids (those are also UUIDs).
"""

from __future__ import annotations

import hashlib
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("plugin.overseer.claude_desktop")


CLAUDE_DESKTOP_SOURCE = "claude-desktop"


def _parse_iso(s: str) -> datetime:
    """Parse ISO 8601 — tolerates trailing 'Z' + microseconds."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _extract_message_text(msg: dict) -> str:
    """Reduce a chat_messages entry to plain text. Tolerates both old
    (text=str) and new (content=[blocks]) export shapes.

    Mirrors claude_jsonl.extract_content_text — tool_use/tool_result/
    image blocks become markers so the summarizer sees the shape of
    the work without the payload.
    """
    # Newer shape: content is a list of typed blocks.
    content = msg.get("content")
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
                parts.append("[tool_use: {}]".format(
                    block.get("name", "?")))
            elif btype == "tool_result":
                parts.append("[tool_result]")
            elif btype == "image":
                parts.append("[image]")
            else:
                parts.append("[{}]".format(btype or "block"))
        joined = "\n".join(p for p in parts if p)
        if joined:
            return joined
    # Older shape: flat text.
    text = msg.get("text")
    if isinstance(text, str):
        return text
    # Final fallback: any 'human_message' / 'assistant_message' field.
    for key in ("human_message", "assistant_message", "message"):
        v = msg.get(key)
        if isinstance(v, str):
            return v
    return ""


def file_sha256(path) -> str:
    """SHA256 of the export ZIP for dedup."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _conversation_to_session(conv: dict) -> tuple[dict, list[dict]]:
    """Convert one export-format conversation into the (metadata,
    messages) shape the rest of the overseer ingest pipeline expects.

    Mirrors parse_claude_code_jsonl's return contract so a future
    "actually ingest" step can feed both sources through the same
    downstream code.
    """
    conv_uuid = str(conv.get("uuid") or "")
    name = str(conv.get("name") or "")
    started_raw = conv.get("created_at")
    ended_raw = conv.get("updated_at") or started_raw
    chat = conv.get("chat_messages") or []

    messages: list[dict] = []
    user_count = 0
    assistant_count = 0
    for m in chat:
        sender = str(m.get("sender") or "")
        # Normalize: Anthropic's export uses "human"/"assistant",
        # the overseer's internal shape uses "user"/"assistant".
        role = "user" if sender == "human" else sender or "user"
        text = _extract_message_text(m)
        ts = m.get("created_at")
        if role == "user":
            user_count += 1
        elif role == "assistant":
            assistant_count += 1
        messages.append({
            "role": role,
            "content_text": text,
            "timestamp": ts,
            "has_tool_use": False,   # desktop conversations don't carry tool_use
            "is_sidechain": False,
        })

    duration_minutes = 0
    if started_raw and ended_raw:
        try:
            d1 = _parse_iso(started_raw)
            d2 = _parse_iso(ended_raw)
            duration_minutes = max(
                0, int((d2 - d1).total_seconds() / 60))
        except Exception as e:
            log.warning("could not compute duration for %s: %s",
                        conv_uuid, e)

    metadata = {
        "session_id": conv_uuid,
        "title": name,
        "started_at": started_raw,
        "ended_at": ended_raw,
        "duration_minutes": duration_minutes,
        "message_count": len(messages),
        "user_message_count": user_count,
        "assistant_message_count": assistant_count,
        "tool_use_count": 0,
        "cwd": None,
        "git_branch": None,
        "version": None,
        "entrypoint": "claude-desktop",
        "parse_errors": 0,
        "total_lines": len(chat),
    }
    return metadata, messages


def parse_claude_desktop_export(zip_path) -> dict:
    """Parse an Anthropic Data Export ZIP.

    Returns a dict shaped for the dry-run endpoint:

      {
        "ok": True,
        "zip_sha256": "...",
        "conversations": [
          {"metadata": {...}, "preview_messages": [first 3 messages]},
          ...
        ],
        "totals": {
          "conversations": int,
          "messages": int,
          "human_messages": int,
          "assistant_messages": int,
        },
        "errors": [...],
      }

    Does NOT write to the DB. That's the next step.
    """
    p = Path(zip_path)
    if not p.is_file():
        raise FileNotFoundError(
            "Anthropic export ZIP not found: {}".format(p))

    out = {
        "ok": True,
        "zip_path": str(p),
        "zip_sha256": file_sha256(p),
        "conversations": [],
        "totals": {
            "conversations": 0,
            "messages": 0,
            "human_messages": 0,
            "assistant_messages": 0,
        },
        "errors": [],
    }

    try:
        with zipfile.ZipFile(p, "r") as z:
            names = z.namelist()
            conv_path = None
            for cand in ("conversations.json", "data/conversations.json"):
                if cand in names:
                    conv_path = cand
                    break
            if not conv_path:
                out["ok"] = False
                out["errors"].append(
                    "conversations.json not found in ZIP. Contents: "
                    + ", ".join(names[:20])
                    + (" ..." if len(names) > 20 else "")
                )
                return out
            with z.open(conv_path) as f:
                raw = f.read()
    except zipfile.BadZipFile as e:
        out["ok"] = False
        out["errors"].append("ZIP could not be opened: {}".format(e))
        return out

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        out["ok"] = False
        out["errors"].append(
            "conversations.json was not valid JSON: {}".format(e))
        return out

    if not isinstance(data, list):
        out["ok"] = False
        out["errors"].append(
            "conversations.json was not a list (got {}); cannot parse".format(
                type(data).__name__))
        return out

    for conv in data:
        if not isinstance(conv, dict):
            out["errors"].append("non-dict conversation entry skipped")
            continue
        try:
            metadata, messages = _conversation_to_session(conv)
        except Exception as e:
            out["errors"].append(
                "failed to parse conversation {}: {}".format(
                    conv.get("uuid"), e))
            continue
        out["conversations"].append({
            "metadata": metadata,
            # Preview only — full ingest is the next slice.
            "preview_messages": [
                {
                    "role": m["role"],
                    "snippet": (m["content_text"] or "")[:200] + (
                        "…" if len(m["content_text"] or "") > 200 else ""
                    ),
                    "timestamp": m["timestamp"],
                }
                for m in messages[:3]
            ],
        })
        out["totals"]["conversations"] += 1
        out["totals"]["messages"] += metadata["message_count"]
        out["totals"]["human_messages"] += metadata["user_message_count"]
        out["totals"]["assistant_messages"] += \
            metadata["assistant_message_count"]

    return out


def claude_desktop_imported_id(session_id: str) -> str:
    """Synthesize the imported_sessions.imported_id key for a desktop
    session. Prefixes with the source label so collisions with
    claude-code session ids (which are also UUIDs) are impossible.
    """
    return "claude-desktop:{}".format(session_id)
