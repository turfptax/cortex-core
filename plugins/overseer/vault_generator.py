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

What this scaffold INTENTIONALLY DOES NOT YET DO (Phase 2.2 follow-ups)
----------------------------------------------------------------------
- **No atomic swap.** Writes directly into the output tree. A failure
  mid-render leaves a partial vault. Acceptable for the scaffold
  because the vault is regenerable; not acceptable for production
  (DESIGN.md §4).
- **No hand-edit preservation.** Always overwrites the loop-owned
  portion. Once Tory starts hand-editing files, the next slice wires
  up the marker-aware merge per DESIGN.md §5.
- **No sensitivity gating.** Emits everything regardless of tier.
  Slice 13 gating wires up next slice — confidential gets sanitized,
  restricted gets excluded.
- **No per-file hash skip.** Always writes every file. The next slice
  reads existing files, compares source_hash, skips if equal.
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


# ── Per-entity renderers ────────────────────────────────────────────


def _render_one(out_path: Path, frontmatter: dict, title: str,
                body_below_marker: str) -> dict:
    """Write a file with frontmatter + marker + loop-owned body. Always
    writes for v1 (no skip-on-hash yet). Returns a small dict with
    bookkeeping fields."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    contents = (
        render_frontmatter(frontmatter)
        + "\n"
        + f"# {title}\n"
        + "\n"
        + MARKER
        + "\n\n"
        + body_below_marker.rstrip()
        + "\n"
    )
    out_path.write_text(contents, encoding="utf-8")
    return {"path": str(out_path), "bytes": len(contents)}


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
    title = row.get("title") or row.get("name") or f"theme-{row.get('id')}"
    slug = slugify(title) or f"theme-{row.get('id')}"
    out = out_root / "abstractions" / "themes" / f"{slug}.md"
    fm = {
        "type": "theme",
        "id": row.get("id"),
        "slug": slug,
        "confidence": row.get("confidence") or "med",
        "created_at": (row.get("created_at") or "")[:10],
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    body = "### Claim\n" + (row.get("body") or "") + "\n"
    return _render_one(out, fm, title, body)


def render_question(out_root: Path, row: dict, render_at: str) -> dict:
    qid = row.get("id")
    body_text = row.get("body") or row.get("question") or ""
    short = short_slug(body_text, max_len=40)
    qslug = f"q{int(qid):03d}-{short}" if qid is not None else f"q-{short}"
    out = out_root / "abstractions" / "questions" / f"{qslug}.md"
    fm = {
        "type": "question",
        "id": qid,
        "slug": qslug,
        "confidence": row.get("confidence") or "med",
        "lifecycle": row.get("lifecycle") or row.get("status") or "active",
        "first_filed": (row.get("created_at") or "")[:10],
        "last_updated": (row.get("updated_at") or
                         row.get("created_at") or "")[:10],
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    title = body_text[:120] + ("…" if len(body_text) > 120 else "")
    body = "### Question\n" + body_text + "\n"
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
    fm = {
        "type": "journal-overseer",
        "tick_id": jid,
        "tick_at": row.get("local_created_at")
                   or row.get("created_at") or "",
        "provenance": row.get("provenance") or row.get("model") or "",
        "source_hash": source_hash(row),
        "render_at": render_at,
    }
    body_text = row.get("entry") or row.get("body") or ""
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
    body_text = row.get("body") or ""
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

    def _bump(key: str):
        counts[key] = counts.get(key, 0) + 1

    def _try(table: str, row: dict, fn):
        try:
            fn(out_root, row, render_at)
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
            render_human_journal_day(out_root, date_str, rows, render_at)
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

    # Last-render meta file
    duration = time.time() - started
    last_render_path = out_root / "_meta" / "last-render.md"
    last_render_path.parent.mkdir(parents=True, exist_ok=True)
    meta_fm = {
        "type": "meta-last-render",
        "render_at": render_at,
        "duration_seconds": round(duration, 2),
        "scaffold_pass": "phase-2-first-pass",
    }
    body_lines = ["## Counts by folder", ""]
    body_lines.append("| Folder | Files written |")
    body_lines.append("|---|---|")
    for k, v in sorted(counts.items()):
        body_lines.append(f"| {k} | {v} |")
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

    log_fn(
        "vault_generator: done in %.2fs (counts=%s, errors=%d)",
        duration, counts, len(errors),
    )
    return {
        "ok": True,
        "out_dir": str(out_root),
        "counts": counts,
        "duration_s": round(duration, 2),
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
        "--out", required=True, help="Output directory for vault tree",
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
