"""Vault generator — Phase 2 scaffold (2026-05-27).

Renders the interpretive corpus from overseer.db to markdown + YAML
frontmatter under a configured output directory. The vault structure
is locked by vault/DESIGN.md.

What this scaffold covers (Phase 2 first pass)
----------------------------------------------
- Walk each interpretive table and emit one .md file per row.
- Frontmatter from the row + computed source_hash for change detection.
- Body = the artifact body + wikilinks to related artifacts.
- A `## Generated below this line — edits above are preserved` marker
  for future hand-edit support.
- One CLI entry point: `python -m plugins.overseer.vault_generator
  --out /path/to/vault`.
- One HTTP route landed alongside: POST /plugins/overseer/vault/render.

Phase 2.2a (2026-05-27) — hash skip + hand-edit preservation
-------------------------------------------------------------
- Per-file hash skip: existing files whose source_hash matches AND
  whose below-marker matches what we'd write now are SKIPPED. Re-
  running with no DB changes is a no-op (DESIGN.md acceptance #2).
- Hand-edit preservation: above-marker content (title + Tory's
  free-text notes) survives every re-render. Below-marker hand-edits
  are detected (existing below-marker differs from new but source
  data is unchanged) and the loop writes its new form to a
  `<name>.loop-update-<timestamp>.md` sibling, never clobbering.
  Siblings are excluded from the orphan sweep so they persist until
  Tory merges by hand (DESIGN.md §4-§5).

What this scaffold STILL DOES NOT YET DO (Phase 2.2b/2.2c)
----------------------------------------------------------
- **No atomic swap.** Writes directly into the output tree. A failure
  mid-render leaves a partial vault. Acceptable for the scaffold
  because the vault is regenerable; not acceptable for production
  (DESIGN.md §4). Phase 2.2b will wrap render in vault.tmp/ then
  atomic rename.
- **No sensitivity gating.** Emits everything regardless of tier.
  Slice 13 gating wires up in Phase 2.2c — confidential gets
  sanitized, restricted gets excluded entirely.
- **No pull_event linkage in rendered bodies.** Rendered files don't
  yet show "drilled into via" sections; that's a Phase 3 read-side
  feature.

Why ship the scaffold anyway
----------------------------
Proves the render shape against real data. Tory can `cd ~/cortex-
vault && tree` after a single run and see what 3,450 gists + 374
journal entries + 214 narratives + 90 patterns/drift/blindspots
actually look like as markdown. That feedback shapes Phase 2.2.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

log = logging.getLogger("plugin.overseer.vault_generator")


# ── Slug helpers ───────────────────────────────────────────────────


_SLUG_NON_WORD = re.compile(r"[^a-z0-9-]+")
_SLUG_DASH_RUNS = re.compile(r"-{2,}")


def slugify(s: str) -> str:
    """Deterministic kebab-slug. ASCII-fold (best effort), lowercase,
    replace non-alphanumerics with `-`, collapse runs, trim."""
    if not s:
        return ""
    s = s.strip().lower()
    s = s.replace("'", "").replace('"', "")
    s = _SLUG_NON_WORD.sub("-", s)
    s = _SLUG_DASH_RUNS.sub("-", s)
    return s.strip("-")


def short_slug(s: str, max_len: int = 40) -> str:
    """Slug truncated to max_len characters at the last word boundary."""
    full = slugify(s)
    if len(full) <= max_len:
        return full
    cut = full[:max_len]
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut


# ── Frontmatter ─────────────────────────────────────────────────────


def _yaml_scalar(v) -> str:
    """Conservative YAML scalar quoting — doesn't ship a YAML library
    dependency. Handles the value types we emit (strings, ints, bools,
    None, lists of strings)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[" + ", ".join(_yaml_scalar(x) for x in v) + "]"
    s = str(v)
    # Quote if there's anything ambiguous.
    if s == "" or any(c in s for c in (":", "#", "\n", "\"", "'", "[", "]", "{", "}", ",")):
        # Use double quotes; escape backslashes + double quotes.
        escaped = s.replace("\\", "\\\\").replace("\"", "\\\"")
        return f'"{escaped}"'
    return s


def render_frontmatter(d: dict) -> str:
    """Render a dict as YAML frontmatter — flat key:value only.
    Lists get the inline `[a, b, c]` form. No nested dicts in v1."""
    lines = ["---"]
    for k, v in d.items():
        lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def source_hash(payload) -> str:
    """sha256 of the canonical-form JSON of the source row(s) that
    produced a file. Used for change detection in the next slice."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _iso_local_now() -> str:
    """Local-time ISO with offset. Pi runs on America/Chicago."""
    now = _dt.datetime.now().astimezone()
    return now.isoformat(timespec="seconds")


MARKER = "## Generated below this line — edits above are preserved"


_SOURCE_HASH_RE = re.compile(
    r"^source_hash:\s*\"?([0-9a-fA-F]+)\"?\s*$", re.MULTILINE)


def _split_existing_file(text: str) -> tuple[str, str, str]:
    """Parse an existing vault file into (above_marker, below_marker,
    source_hash).

    above_marker  — title line + any of Tory's free-text edits between
                    the frontmatter end and the MARKER. Loop preserves
                    this verbatim across re-renders (DESIGN.md §4).
    below_marker  — loop-owned content after the marker. Loop may
                    rewrite if source data has changed.
    source_hash   — the hash recorded in the existing frontmatter,
                    extracted via regex to avoid a YAML dependency.

    A missing marker (legacy file or hand-stripped) collapses to
    above_marker = entire post-frontmatter body, below_marker = "".
    """
    if text.startswith("---\n"):
        fm_end = text.find("\n---\n", 4)
        if fm_end >= 0:
            fm_text = text[:fm_end + 5]
            after_fm = text[fm_end + 5:].lstrip("\n")
        else:
            fm_text = ""
            after_fm = text
    else:
        fm_text = ""
        after_fm = text

    source_hash_val = ""
    m = _SOURCE_HASH_RE.search(fm_text)
    if m:
        source_hash_val = m.group(1)

    marker_idx = after_fm.find(MARKER)
    if marker_idx >= 0:
        above = after_fm[:marker_idx].rstrip()
        below_start = marker_idx + len(MARKER)
        below = after_fm[below_start:].lstrip("\n").rstrip()
    else:
        above = after_fm.rstrip()
        below = ""

    return above, below, source_hash_val


# ── Per-entity renderers ────────────────────────────────────────────


def _render_one(out_path: Path, frontmatter: dict, title: str,
                body_below_marker: str) -> dict:
    """Write a file with frontmatter + marker + loop-owned body.

    Phase 2.2a (2026-05-27) — hash-skip + hand-edit preservation:

    - **Fresh file** (destination doesn't exist): write the canonical
      form (frontmatter + title + marker + new body).
    - **Existing file, source unchanged AND below-marker unchanged**:
      skip the write entirely. Re-running with no DB changes is a
      no-op (DESIGN.md acceptance criterion #2).
    - **Existing file, source unchanged but below-marker differs**:
      Tory edited the loop output below the marker. Write the loop's
      new form to a `<name>.loop-update-<timestamp>.md` sibling and
      leave the human-edited file intact (DESIGN.md §4-§5).
    - **Existing file, source changed**: write a merged file —
      new frontmatter + Tory's preserved above-marker content + new
      below-marker. Above-marker edits survive every re-render.

    Returns a dict with `path`, `bytes`, `action`. `action` is one of:
      "written"               — fresh or merged write
      "skipped"               — true no-op
      "sibling_for_handedit"  — wrote loop's new form to a sibling
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    new_fm_text = render_frontmatter(frontmatter)
    new_below = body_below_marker.rstrip()
    title_block = f"# {title}"

    fresh_contents = (
        new_fm_text + "\n"
        + title_block + "\n"
        + "\n"
        + MARKER + "\n\n"
        + new_below + "\n"
    )

    if not out_path.exists():
        out_path.write_text(fresh_contents, encoding="utf-8")
        return {"path": str(out_path.resolve()),
                "bytes": len(fresh_contents),
                "action": "written"}

    existing_text = out_path.read_text(encoding="utf-8")
    above_existing, below_existing, existing_source_hash = \
        _split_existing_file(existing_text)

    new_source_hash = str(frontmatter.get("source_hash") or "")
    hash_match = (new_source_hash != ""
                  and new_source_hash == existing_source_hash)
    below_match = (below_existing.strip() == new_below.strip())

    if hash_match and below_match:
        # Truly unchanged. No write at all.
        return {"path": str(out_path.resolve()),
                "bytes": 0,
                "action": "skipped"}

    if hash_match and not below_match:
        # Source data unchanged but the existing below-marker differs
        # from what we'd write now → the human edited the loop output.
        # Write the loop's new form to a sibling, never clobber.
        ts = _dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        sibling_name = (
            out_path.stem + f".loop-update-{ts}" + out_path.suffix)
        sibling_path = out_path.with_name(sibling_name)
        sibling_path.write_text(fresh_contents, encoding="utf-8")
        return {"path": str(out_path.resolve()),
                "bytes": len(fresh_contents),
                "action": "sibling_for_handedit",
                "sibling": str(sibling_path.resolve())}

    # Source data changed (hash differs) → merged write:
    # new frontmatter + preserved above-marker + new below-marker.
    merged_above = above_existing.strip() or title_block
    merged_contents = (
        new_fm_text + "\n"
        + merged_above + "\n"
        + "\n"
        + MARKER + "\n\n"
        + new_below + "\n"
    )
    out_path.write_text(merged_contents, encoding="utf-8")
    return {"path": str(out_path.resolve()),
            "bytes": len(merged_contents),
            "action": "written"}


# Folder roots the generator owns. Any *.md inside these folders that
# isn't in the per-run manifest is an orphan (its source row was
# deleted or renamed since the previous render). The orchestrator
# sweeps these folders at end-of-render and deletes orphans.
#
# IMPORTANT: README.md + _meta/last-render.md are loop-owned but
# emitted *after* the per-table renders, so they're added to the
# manifest separately.
_GHOST_SWEEP_ROOTS = (
    "abstractions/projects",
    "abstractions/people",
    "abstractions/themes",
    "abstractions/patterns",
    "abstractions/drift",
    "abstractions/questions",
    "gists",
    "journal/daily",
    "journal/overseer",
    "narratives/daily",
    "narratives/weekly",
    "narratives/monthly",
    "narratives/yearly",
    "notes-for-future-overseer",
)


_LOOP_UPDATE_SIBLING_RE = re.compile(r"\.loop-update-\d{8}T\d{6}\.md$")


def _sweep_orphans(out_root: Path, manifest: set) -> dict:
    """Walk the owned folders, delete any *.md file whose absolute
    path isn't in `manifest`. Returns counts for the meta file.

    A safety floor: never deletes if the manifest is empty (would
    nuke the whole vault) and never deletes files outside the
    owned folders.

    Phase 2.2a (2026-05-27): *.loop-update-<ts>.md siblings are
    excluded from the sweep — they're created when the loop detects
    a hand-edited below-marker conflict and live until Tory merges
    them by hand. Auto-deleting them would silently destroy his
    unfinished merges.
    """
    if not manifest:
        return {"orphans_deleted": 0, "orphans_skipped_no_manifest": 1,
                "orphan_files": [], "siblings_preserved": 0}
    deleted: list = []
    siblings_preserved = 0
    for sub in _GHOST_SWEEP_ROOTS:
        folder = out_root / sub
        if not folder.exists():
            continue
        for md in folder.rglob("*.md"):
            try:
                abs_path = str(md.resolve())
            except Exception:
                continue
            if abs_path in manifest:
                continue
            # Phase 2.2a: protect loop-update siblings from the sweep.
            if _LOOP_UPDATE_SIBLING_RE.search(md.name):
                siblings_preserved += 1
                continue
            try:
                md.unlink()
                deleted.append(str(md.relative_to(out_root)))
            except Exception as e:
                log.warning("ghost-sweep: could not delete %s: %s",
                            md, e)
    return {"orphans_deleted": len(deleted),
            "orphan_files": deleted,
            "siblings_preserved": siblings_preserved}


def render_project(out_root: Path, row: dict, render_at: str) -> dict:
    tag = row.get("project") or row.get("tag") or "unknown"
    slug = slugify(tag) or "unknown"
    out = out_root / "abstractions" / "projects" / f"{slug}.md"
    fm = {
        "type": "project",
        "tag": tag,
        "status": row.get("status") or "active",
        "category": row.get("category") or "",
        "session_count": row.get("session_count") or 0,
        "total_minutes_active": row.get("active_minutes_total") or 0,
        "last_touched": (row.get("last_active_at") or "")[:10],
        "sensitivity": row.get("sensitivity") or "internal",
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    body = (
        "### Narrative\n"
        + (row.get("narrative_text") or "_(no narrative authored yet)_")
        + "\n"
    )
    return _render_one(out, fm, tag, body)


def render_theme(out_root: Path, row: dict, render_at: str) -> dict:
    # summaries_theme schema: title (NOT NULL), body, confidence,
    # first_seen_at, last_reinforced_at, created_at. L99 fix 2026-05-27.
    title = row.get("title") or f"theme-{row.get('id')}"
    slug = slugify(title) or f"theme-{row.get('id')}"
    out = out_root / "abstractions" / "themes" / f"{slug}.md"
    fm = {
        "type": "theme",
        "id": row.get("id"),
        "slug": slug,
        "confidence": row.get("confidence") or "med",
        "first_seen_at": (row.get("first_seen_at") or "")[:10],
        "last_reinforced_at": (row.get("last_reinforced_at") or "")[:10],
        "created_at": (row.get("created_at") or "")[:10],
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    body = "### Claim\n" + (row.get("body") or "") + "\n"
    return _render_one(out, fm, title, body)


def render_question(out_root: Path, row: dict, render_at: str) -> dict:
    # open_questions schema: question (NOT NULL — the actual text),
    # body (longer body, default ""), confidence, lifecycle,
    # first_observed_at, last_observed_at, evidence_count. L99 fix
    # 2026-05-27: title was reading .body (often empty); should use
    # .question as primary.
    qid = row.get("id")
    question_text = row.get("question") or row.get("body") or ""
    body_text = row.get("body") or ""
    short = short_slug(question_text, max_len=40)
    qslug = f"q{int(qid):03d}-{short}" if qid is not None else f"q-{short}"
    out = out_root / "abstractions" / "questions" / f"{qslug}.md"
    fm = {
        "type": "question",
        "id": qid,
        "slug": qslug,
        "confidence": row.get("confidence") or "med",
        "lifecycle": row.get("lifecycle") or "active",
        "is_active": bool(row.get("is_active", 1)),
        "evidence_count": row.get("evidence_count") or 0,
        "first_filed": (row.get("first_observed_at") or "")[:10],
        "last_updated": (row.get("last_observed_at") or "")[:10],
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    title = question_text[:120] + ("…" if len(question_text) > 120 else "")
    body = ("### Question\n" + question_text + "\n"
            + (("\n### Notes\n" + body_text + "\n") if body_text else ""))
    return _render_one(out, fm, title, body)


def render_pattern(out_root: Path, row: dict, render_at: str) -> dict:
    pid = row.get("id")
    name = row.get("name") or row.get("title") or f"pattern-{pid}"
    short = short_slug(name, max_len=40)
    pslug = f"pattern-{pid}-{short}" if pid is not None else f"pattern-{short}"
    out = out_root / "abstractions" / "patterns" / f"{pslug}.md"
    fm = {
        "type": "pattern",
        "id": pid,
        "slug": pslug,
        "name": name,
        "confidence": row.get("confidence") or "med",
        "occurrences": row.get("occurrences") or 1,
        "first_observed_at": (row.get("first_observed_at") or "")[:10],
        "last_observed_at": (row.get("last_observed_at") or "")[:10],
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    body = "### Description\n" + (row.get("body") or "") + "\n"
    return _render_one(out, fm, name, body)


def render_drift(out_root: Path, row: dict, render_at: str) -> dict:
    did = row.get("id")
    body_text = row.get("body") or ""
    short = short_slug(body_text, max_len=40)
    dslug = f"drift-{did}-{short}" if did is not None else f"drift-{short}"
    out = out_root / "abstractions" / "drift" / f"{dslug}.md"
    fm = {
        "type": "drift",
        "id": did,
        "slug": dslug,
        "direction": row.get("direction") or "",
        "confidence": row.get("confidence") or "med",
        "observed_at": (row.get("observed_at") or "")[:10],
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    title = body_text[:120] + ("…" if len(body_text) > 120 else "")
    body = "### Observation\n" + body_text + "\n"
    return _render_one(out, fm, title, body)


def render_future_note(out_root: Path, row: dict, render_at: str) -> dict:
    nid = row.get("id")
    written = (row.get("written_at") or "")[:10] or "unknown-date"
    body_text = row.get("body") or ""
    short = short_slug(body_text, max_len=40) or f"note-{nid}"
    fname = f"{written}-{short}.md"
    out = out_root / "notes-for-future-overseer" / fname
    fm = {
        "type": "future-note",
        "id": nid,
        "author_instance": row.get("instance_id") or "unknown",
        "written_at": row.get("written_at") or "",
        "source_hash": source_hash(row),
    }
    title = body_text.split("\n", 1)[0][:80]
    return _render_one(out, fm, title or f"future-note-{nid}", body_text)


def render_overseer_journal(out_root: Path, row: dict,
                              render_at: str) -> dict:
    jid = row.get("id")
    tick_id_zero_padded = f"{int(jid):05d}" if jid is not None else "00000"
    out = (out_root / "journal" / "overseer"
           / f"tick-{tick_id_zero_padded}.md")
    # overseer_journal columns confirmed 2026-05-27 (L99): written_at +
    # local_written_at carry the tick time (NOT created_at); body
    # carries the entry text. Earlier .entry fallback would have
    # produced empty bodies if .body wasn't there.
    fm = {
        "type": "journal-overseer",
        "tick_id": jid,
        "tick_at": row.get("local_written_at")
                   or row.get("written_at") or "",
        "provenance": row.get("model") or row.get("backend") or "",
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    body_text = row.get("body") or row.get("entry") or ""
    title = body_text.split("\n", 1)[0][:80] or f"tick #{jid}"
    return _render_one(out, fm, title, body_text)


def render_temporal_narrative(out_root: Path, row: dict,
                                render_at: str) -> dict:
    kind = row.get("kind") or "weekly"
    label = row.get("period_label") or "unknown"
    folder = (out_root / "narratives"
              / {"daily": "daily", "weekly": "weekly",
                 "monthly": "monthly", "yearly": "yearly"}.get(
                  kind, "weekly"))
    out = folder / f"{label}.md"
    fm = {
        "type": f"narrative-{kind}",
        "period_label": label,
        "period_start": row.get("period_start") or "",
        "period_end": row.get("period_end") or "",
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    title = label
    # Column is `narrative` not `body` — L99 fix 2026-05-27. Earlier
    # render emitted 215 empty narrative files because we read .body.
    body_text = row.get("narrative") or row.get("body") or ""
    return _render_one(out, fm, title, body_text)


def render_human_journal_day(out_root: Path, date_str: str,
                              rows: list, render_at: str) -> dict:
    """All human_journal_entries for one date, grouped into one file."""
    out = out_root / "journal" / "daily" / f"{date_str}.md"
    fm = {
        "type": "journal-daily",
        "date": date_str,
        "entry_count": len(rows),
        "source_hash": source_hash([dict(r) for r in rows]),
        "render_at": render_at,
    }
    parts = []
    for r in rows:
        when = (r.get("local_created_at")
                or r.get("created_at") or "")[11:16] or "??:??"
        entry_type = r.get("entry_type") or "free"
        text = r.get("text") or ""
        parts.append(f"### {when} — {entry_type}\n\n{text}\n")
    body_text = "\n".join(parts)
    return _render_one(out, fm, date_str, body_text)


def render_gist(out_root: Path, row: dict, render_at: str) -> dict:
    gid = row.get("id")
    created = (row.get("local_created_at")
               or row.get("created_at") or "")[:10] or "unknown-date"
    parts = created.split("-")
    if len(parts) >= 3:
        year, month, day = parts[0], parts[1], parts[2]
        bucket = out_root / "gists" / year / month / day
    else:
        bucket = out_root / "gists" / "unsorted"
    fname = f"g{gid}.md"
    out = bucket / fname
    fm = {
        "type": "gist",
        "id": gid,
        "period_label": row.get("period_label") or "",
        "category": row.get("category") or "",
        "confidence": row.get("confidence") or "med",
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    body_text = row.get("body") or ""
    title = f"g{gid} — " + (body_text[:80] + (
        "…" if len(body_text) > 80 else ""))
    body = "### Body\n" + body_text + "\n"
    return _render_one(out, fm, title, body)


# ── Top-level orchestrator ──────────────────────────────────────────


def render_vault(db, out_dir: str, *,
                  gist_limit: int = 0,
                  log_fn=None) -> dict:
    """Render the full vault. Always writes (no hash skip yet).

    Args:
      db: an OverseerDB instance.
      out_dir: filesystem path to render into. Created if missing.
                Caller MUST pass an absolute path readable by the
                human user — the caller knows whose home it lives
                under; the service may run as root, so `~` expansion
                here lands wherever the SERVICE thinks home is, not
                where the human user expects.
      gist_limit: max gists to render (0 = all). v1 default = all.
      log_fn: optional callable for progress logging.

    Returns a dict with:
      ok            — bool
      out_dir       — absolute path
      counts        — files written per folder
      duration_s    — total render time
      errors        — list of (table, id, error_msg) tuples
    """
    out_root = Path(out_dir).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    log_fn = log_fn or log.info

    started = time.time()
    counts: dict = {}
    errors: list = []
    render_at = _iso_local_now()
    # Manifest of every absolute file path the loop OWNS this run.
    # Note that the manifest is "owned this run", not "written this
    # run" — files whose source data is unchanged are skipped (action
    # = "skipped") but still listed in the manifest so the orphan
    # sweep doesn't delete them.
    manifest: set = set()
    # Phase 2.2a (2026-05-27): per-action accounting. Re-running with
    # no DB changes should report actions={"skipped": N, "written": 0}.
    actions: dict = {"written": 0, "skipped": 0, "sibling_for_handedit": 0}
    sibling_files: list = []

    def _bump(key: str):
        counts[key] = counts.get(key, 0) + 1

    def _try(table: str, row: dict, fn):
        try:
            result = fn(out_root, row, render_at)
            if isinstance(result, dict) and result.get("path"):
                manifest.add(result["path"])
            action = (result or {}).get("action") or "written"
            actions[action] = actions.get(action, 0) + 1
            sibling = (result or {}).get("sibling")
            if sibling:
                sibling_files.append(sibling)
            _bump(table)
        except Exception as e:
            errors.append((table, row.get("id"), str(e)))
            log.warning("render %s/%s failed: %s",
                        table, row.get("id"), e)

    log_fn("vault_generator: rendering projects…")
    for row in db.list_project_summaries(order_by="last_active_at",
                                          descending=True):
        _try("projects", row, render_project)

    log_fn("vault_generator: rendering themes…")
    for row in db.recent_themes(limit=500):
        _try("themes", row, render_theme)

    log_fn("vault_generator: rendering questions…")
    for row in db.active_questions(limit=500):
        _try("questions", row, render_question)

    log_fn("vault_generator: rendering patterns…")
    for row in db.recent_patterns(limit=500):
        _try("patterns", row, render_pattern)

    log_fn("vault_generator: rendering drift…")
    for row in db.recent_drift(limit=500):
        _try("drift", row, render_drift)

    log_fn("vault_generator: rendering future-overseer notes…")
    for row in db.all_future_notes():
        _try("future_notes", row, render_future_note)

    log_fn("vault_generator: rendering overseer journal…")
    for row in db.recent_journal_entries(limit=2000):
        _try("overseer_journal", row, render_overseer_journal)

    log_fn("vault_generator: rendering temporal narratives…")
    for row in db.list_temporal_narratives(limit=1000):
        _try("narratives", row, render_temporal_narrative)

    log_fn("vault_generator: rendering human journal entries…")
    by_day: dict = {}
    for row in db.list_human_journal_entries(limit=10000):
        date_key = (row.get("local_created_at")
                    or row.get("created_at") or "")[:10]
        if not date_key:
            continue
        by_day.setdefault(date_key, []).append(row)
    for date_str, rows in by_day.items():
        try:
            result = render_human_journal_day(
                out_root, date_str, rows, render_at)
            if isinstance(result, dict) and result.get("path"):
                manifest.add(result["path"])
            action = (result or {}).get("action") or "written"
            actions[action] = actions.get(action, 0) + 1
            sibling = (result or {}).get("sibling")
            if sibling:
                sibling_files.append(sibling)
            _bump("human_journal_days")
        except Exception as e:
            errors.append(("human_journal_days", date_str, str(e)))

    log_fn("vault_generator: rendering gists (may take a moment)…")
    # Pull gists in pages to avoid loading 3,450+ rows at once.
    page = 500
    offset = 0
    total_rendered = 0
    while True:
        rows = db._conn.execute(
            "SELECT * FROM summaries_gist ORDER BY id DESC "
            "LIMIT ? OFFSET ?",
            (page, offset),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            row = dict(r)
            _try("gists", row, render_gist)
            total_rendered += 1
            if gist_limit and total_rendered >= gist_limit:
                break
        if gist_limit and total_rendered >= gist_limit:
            break
        offset += page

    # Orphan sweep BEFORE writing meta/README so the sweep doesn't
    # delete those (they're added to manifest after this point).
    sweep_result = _sweep_orphans(out_root, manifest)
    if sweep_result.get("orphans_deleted"):
        log_fn("vault_generator: orphan sweep removed %d files",
                sweep_result["orphans_deleted"])

    # Last-render meta file
    duration = time.time() - started
    last_render_path = out_root / "_meta" / "last-render.md"
    last_render_path.parent.mkdir(parents=True, exist_ok=True)
    meta_fm = {
        "type": "meta-last-render",
        "render_at": render_at,
        "duration_seconds": round(duration, 2),
        "scaffold_pass": "phase-2.2a",
        "files_written": actions.get("written", 0),
        "files_skipped": actions.get("skipped", 0),
        "siblings_for_handedit": actions.get("sibling_for_handedit", 0),
    }
    body_lines = ["## Actions this render", ""]
    body_lines.append(
        f"- **Written**: {actions.get('written', 0)} "
        f"(fresh or merged after source change)")
    body_lines.append(
        f"- **Skipped**: {actions.get('skipped', 0)} "
        f"(unchanged — hash + below-marker matched)")
    body_lines.append(
        f"- **Sibling for hand-edit**: "
        f"{actions.get('sibling_for_handedit', 0)} "
        f"(below-marker had been hand-edited; loop's new form written "
        f"to a *.loop-update-*.md sibling)")
    if sibling_files:
        body_lines.append("")
        body_lines.append("### Hand-edit sibling files awaiting manual merge")
        body_lines.append("")
        for s in sibling_files[:50]:
            body_lines.append(f"- {s}")
        if len(sibling_files) > 50:
            body_lines.append(f"- … +{len(sibling_files) - 50} more")
    body_lines.append("")
    body_lines.append("## Counts by folder")
    body_lines.append("")
    body_lines.append("| Folder | Owned (manifest) |")
    body_lines.append("|---|---|")
    for k, v in sorted(counts.items()):
        body_lines.append(f"| {k} | {v} |")
    orphans_deleted = sweep_result.get("orphans_deleted", 0)
    orphan_files = sweep_result.get("orphan_files", [])
    siblings_preserved = sweep_result.get("siblings_preserved", 0)
    body_lines.append("")
    body_lines.append(
        f"## Orphan sweep: {orphans_deleted} file(s) deleted "
        f"({siblings_preserved} loop-update siblings preserved)")
    if orphan_files:
        body_lines.append("")
        for f in orphan_files[:50]:
            body_lines.append(f"- {f}")
        if len(orphan_files) > 50:
            body_lines.append(f"- … +{len(orphan_files) - 50} more")
    if errors:
        body_lines.append("")
        body_lines.append("## Errors")
        body_lines.append("")
        for tbl, rid, msg in errors[:50]:
            body_lines.append(f"- {tbl}#{rid}: {msg}")
        if len(errors) > 50:
            body_lines.append(f"- … +{len(errors) - 50} more")
    last_render_path.write_text(
        render_frontmatter(meta_fm) + "\n# Last render\n\n"
        + "\n".join(body_lines) + "\n",
        encoding="utf-8",
    )

    # L99 must-fix #D (2026-05-27): emit a card-catalog README at the
    # vault root so external AIs landing here have a starting point.
    # Plain markdown — works in any viewer.
    readme_path = out_root / "README.md"
    readme_text = (
        "# Cortex Vault — rendered output\n\n"
        f"*Rendered: {render_at}*\n\n"
        "This directory is the human/AI-readable mirror of Cortex's\n"
        "interpretive memory layer. Files are markdown + YAML\n"
        "frontmatter, generated from `overseer.db` on the Pi.\n\n"
        "## Where to start (for external AIs)\n\n"
        "1. **`abstractions/projects/`** — what Tory is working on,\n"
        "   ordered by recent activity.\n"
        "2. **`abstractions/themes/`** — recurring patterns the\n"
        "   overseer has identified across sessions.\n"
        "3. **`abstractions/questions/`** — open questions Tory is\n"
        "   working through, with evidence trails.\n"
        "4. **`narratives/weekly/`** — most-recent-first temporal\n"
        "   synthesis. Read the last 2-3 weeks to anchor in time.\n"
        "5. **`journal/overseer/`** — overseer's tick-by-tick\n"
        "   reflection (technical thinking layer).\n"
        "6. **`journal/daily/`** — Tory's own daily journal entries.\n"
        "7. **`_meta/last-render.md`** — staleness check (this file).\n\n"
        "## Counts in this render\n\n"
        "| Folder | Files |\n"
        "|---|---|\n"
        + "\n".join(f"| {k} | {v} |" for k, v in sorted(counts.items()))
        + "\n\n"
        "## Constraints\n\n"
        "- **Hand-edit preservation IS live** (Phase 2.2a,\n"
        "  2026-05-27). Edits ABOVE the `## Generated below this\n"
        "  line` marker survive every re-render. Edits BELOW the\n"
        "  marker cause the loop to write its new form to a\n"
        "  `<name>.loop-update-<timestamp>.md` sibling rather than\n"
        "  clobber your edit; merge by hand and delete the sibling.\n"
        "- **No sensitivity gating yet.** Content tier is rendered\n"
        "  as-is. Do not push this directory to a public location\n"
        "  until Phase 2.2c sensitivity gating ships.\n"
        "- **No atomic swap yet.** Mid-render failure can leave the\n"
        "  tree partial. Phase 2.2b will wrap the render in a tmp\n"
        "  directory + atomic rename.\n"
        "- **Source of truth lives in `overseer.db`** — this is a\n"
        "  view, not the canonical store.\n\n"
        "## How to drill deeper than this rendered view\n\n"
        "- `cortex_search(query)` MCP tool — substring search\n"
        "  across all interpretive tables, returns drill-down\n"
        "  tokens.\n"
        "- `cortex_overseer_detail(token)` MCP tool — resolve any\n"
        "  drill token to its full row + linked artifacts.\n"
        "- `overseer_chat(message)` MCP tool — ask the overseer\n"
        "  directly; it renders working memory and answers.\n\n"
        "Phase 3 ships vault-aware tools (`cortex_read`,\n"
        "`cortex_list`, `cortex_graph`, `cortex_recent`) that read\n"
        "this directory directly.\n"
    )
    readme_path.write_text(readme_text, encoding="utf-8")

    log_fn(
        "vault_generator: done in %.2fs (counts=%s, errors=%d)",
        duration, counts, len(errors),
    )
    return {
        "ok": True,
        "out_dir": str(out_root),
        "counts": counts,
        "actions": actions,
        "sibling_files": sibling_files[:50],
        "duration_s": round(duration, 2),
        "orphans_deleted": sweep_result.get("orphans_deleted", 0),
        "orphan_files": sweep_result.get("orphan_files", [])[:20],
        "siblings_preserved": sweep_result.get("siblings_preserved", 0),
        "errors": [
            {"table": t, "id": i, "msg": m}
            for (t, i, m) in errors[:50]
        ],
        "error_count": len(errors),
    }


# ── CLI ─────────────────────────────────────────────────────────────


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render the Cortex vault from overseer.db.")
    parser.add_argument(
        "--db",
        default="/home/turfptax/cortex-core/plugins/overseer/data/overseer.db",
        help="Path to overseer.db",
    )
    parser.add_argument(
        "--out", required=True,
        help=(
            "Output directory for vault tree. The service runs as root "
            "on the Pi — pass an absolute path under the human user's "
            "home (e.g. /home/turfptax/cortex-vault) so the user can "
            "read the output without sudo. ~ expansion resolves to the "
            "running user's home, not the human user's home."
        ),
    )
    parser.add_argument(
        "--gist-limit", type=int, default=0,
        help="Cap on gists rendered (0 = all). Default: all.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # OverseerDB module lives next to this file.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from overseer_db import OverseerDB

    db = OverseerDB(args.db)
    try:
        result = render_vault(
            db, args.out, gist_limit=args.gist_limit,
        )
    finally:
        db.close()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
