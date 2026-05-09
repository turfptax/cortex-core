"""Project rollup data layer (Slice 4 CP1a).

For each Claude Code project the user has imported, compute a
deterministic stats rollup over imported_sessions and write it to
project_summaries. This is the data backbone for the new Projects
tab (CP2). The LLM narrative + loop integration land in CP1b.

INPUTS
  - imported_sessions table (per-session metadata)
  - imported_sessions.metadata_json — extended stats from
    claude_jsonl.extract_extended_stats: tokens, models_used,
    file_paths. Backfill script populates these on existing rows;
    new imports get them set during ingest (still TODO — separate
    edit, see refresh_session_extended_stats below).

OUTPUTS
  - project_summaries table — one row per project, all columns
    populated except narrative/* (those are CP1b).

NO LLM calls in CP1a. Pure aggregation. Cheap to run nightly or
on-demand.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

import claude_jsonl
import pricing


log = logging.getLogger("plugin.overseer.project_summary")


# Cap top_files at this many entries when storing in JSON. UI can
# render fewer; the underlying counts are also accessible via the
# per-session metadata if a future view wants finer granularity.
TOP_FILES_KEEP = 10


# When this key is present in imported_sessions.metadata_json the
# backfill knows the row already has extended stats and can skip
# re-parsing the .jsonl. Removing this key forces a re-parse.
EXTENDED_STATS_VERSION_KEY = "extended_stats_v"
EXTENDED_STATS_VERSION = 1


# ── Per-session extended-stats writer ───────────────────────────


def refresh_session_extended_stats(*, db, imported_id, force=False):
    """Parse the .jsonl for one imported_session and stash the
    extended stats (tokens, models, file_paths) into its
    metadata_json. Idempotent — skips if already present unless
    force=True.

    Returns dict with {ok, updated, error}.
    """
    row = db.get_imported_by_id(imported_id)
    if not row:
        return {"ok": False, "error": "imported row not found"}

    try:
        meta = json.loads(row.get("metadata_json") or "{}")
    except Exception:
        meta = {}

    if not force and meta.get(EXTENDED_STATS_VERSION_KEY) == EXTENDED_STATS_VERSION:
        return {"ok": True, "updated": False,
                "note": "already has extended stats"}

    src = row.get("source_path") or ""
    if not src:
        return {"ok": False, "error": "no source_path"}

    try:
        extended = claude_jsonl.extract_extended_stats(src)
    except FileNotFoundError as e:
        # File may have been deleted on the host. Mark with a
        # sentinel so we don't keep retrying.
        meta[EXTENDED_STATS_VERSION_KEY] = EXTENDED_STATS_VERSION
        meta["extended_stats_error"] = "file not found"
        db._conn.execute(
            "UPDATE imported_sessions SET metadata_json = ? WHERE id = ?",
            (json.dumps(meta), imported_id),
        )
        db._safe_commit()
        return {"ok": False, "error": str(e)}
    except Exception as e:
        log.exception("extract_extended_stats failed for %s: %s",
                      imported_id, e)
        return {"ok": False, "error": str(e)}

    meta.update(extended)
    meta[EXTENDED_STATS_VERSION_KEY] = EXTENDED_STATS_VERSION
    meta.pop("extended_stats_error", None)

    db._conn.execute(
        "UPDATE imported_sessions SET metadata_json = ? WHERE id = ?",
        (json.dumps(meta), imported_id),
    )
    db._safe_commit()
    return {"ok": True, "updated": True}


# ── Project-level aggregation ───────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_safe(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def compute_project_stats(db, project: str) -> dict:
    """Aggregate imported_sessions for one project into a stats dict.

    Returns the dict suitable to upsert directly into
    project_summaries via OverseerDB.upsert_project_summary.
    Pulls extended stats (tokens, models_used, file_paths) from
    each row's metadata_json — rows without extended stats are
    counted in deterministic columns (session_count, minutes) but
    don't contribute to token/cost/file metrics. The
    `cost_known_complete` flag goes 0 if any included row mixes in
    a model that pricing.PRICE_TABLE doesn't recognize.

    Days-active counters use distinct started_at calendar dates.
    """
    rows = db.imported_sessions_for_project(project)
    if not rows:
        return _empty_summary()

    session_count = len(rows)
    total_messages = 0
    total_user = 0
    total_asst = 0
    tool_use_count = 0
    total_minutes = 0
    minutes_per_session: list[int] = []
    active_minutes_total = 0
    active_minutes_per_session: list[int] = []
    tokens_in = 0
    tokens_out = 0
    tokens_cc = 0
    tokens_cr = 0
    models_agg: Counter = Counter()
    files_agg: Counter = Counter()

    starts: list[str] = []
    days_seen: set[str] = set()
    earliest: str | None = None
    latest: str | None = None

    for r in rows:
        total_messages += int(r.get("message_count") or 0)
        total_user += int(r.get("user_message_count") or 0)
        total_asst += int(r.get("assistant_message_count") or 0)
        tool_use_count += int(r.get("tool_use_count") or 0)
        m = int(r.get("duration_minutes") or 0)
        total_minutes += m
        minutes_per_session.append(m)

        started = r.get("started_at")
        if started:
            starts.append(started)
            days_seen.add(started[:10])  # 'YYYY-MM-DD'
            if earliest is None or started < earliest:
                earliest = started
        ended = r.get("ended_at") or started
        if ended:
            if latest is None or ended > latest:
                latest = ended

        # Extended stats from metadata_json
        try:
            meta = json.loads(r.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        if meta.get(EXTENDED_STATS_VERSION_KEY) != EXTENDED_STATS_VERSION:
            continue  # row hasn't been backfilled yet

        tokens_in += int(meta.get("tokens_input_total") or 0)
        tokens_out += int(meta.get("tokens_output_total") or 0)
        tokens_cc += int(meta.get("tokens_cache_creation_total") or 0)
        tokens_cr += int(meta.get("tokens_cache_read_total") or 0)

        # Active minutes — present on rows backfilled with the CP1b
        # parser. Rows from the original CP1a backfill have it as 0;
        # the next backfill --force will populate them.
        am = int(meta.get("active_minutes") or 0)
        active_minutes_total += am
        active_minutes_per_session.append(am)

        for model_id, count in (meta.get("models_used") or {}).items():
            models_agg[model_id] += int(count)

        for path, hits in (meta.get("file_paths") or {}).items():
            files_agg[path] += int(hits)

    avg_minutes = (total_minutes / session_count) if session_count else 0.0
    median_minutes = (
        statistics.median(minutes_per_session) if minutes_per_session else 0.0
    )
    avg_active_minutes = (
        active_minutes_total / session_count if session_count else 0.0
    )
    median_active_minutes = (
        statistics.median(active_minutes_per_session)
        if active_minutes_per_session else 0.0
    )

    cost_usd, has_unknown = pricing.estimate_cost_from_totals(
        models_used=dict(models_agg),
        tokens_input_total=tokens_in,
        tokens_output_total=tokens_out,
        tokens_cache_creation_total=tokens_cc,
        tokens_cache_read_total=tokens_cr,
    )

    days_active_30, days_active_90 = _days_active_within(days_seen)
    lifespan_days = _lifespan_days(earliest, latest)

    top_files = [
        {"path": p, "hits": int(h)}
        for p, h in files_agg.most_common(TOP_FILES_KEEP)
    ]

    return {
        "session_count": session_count,
        "total_messages": total_messages,
        "total_user_messages": total_user,
        "total_assistant_messages": total_asst,
        "tool_use_message_count": tool_use_count,
        "total_minutes": total_minutes,
        "active_minutes_total": active_minutes_total,
        "avg_minutes_per_session": round(float(avg_minutes), 2),
        "median_minutes_per_session": round(float(median_minutes), 2),
        "avg_active_minutes_per_session": round(float(avg_active_minutes), 2),
        "median_active_minutes_per_session": round(float(median_active_minutes), 2),
        "total_tokens_input": tokens_in,
        "total_tokens_output": tokens_out,
        "total_tokens_cache_creation": tokens_cc,
        "total_tokens_cache_read": tokens_cr,
        "cost_usd_estimate": round(float(cost_usd), 4),
        "cost_known_complete": 0 if has_unknown else 1,
        "first_active_at": earliest,
        "last_active_at": latest,
        "days_active_30": days_active_30,
        "days_active_90": days_active_90,
        "days_active_lifespan": lifespan_days,
        "top_files_json": json.dumps(top_files),
        "models_used_json": json.dumps(dict(models_agg)),
    }


def _empty_summary() -> dict:
    """All-zero shape for projects with no imported sessions yet."""
    return {
        "session_count": 0,
        "total_messages": 0,
        "total_user_messages": 0,
        "total_assistant_messages": 0,
        "tool_use_message_count": 0,
        "total_minutes": 0,
        "active_minutes_total": 0,
        "avg_minutes_per_session": 0.0,
        "median_minutes_per_session": 0.0,
        "avg_active_minutes_per_session": 0.0,
        "median_active_minutes_per_session": 0.0,
        "total_tokens_input": 0,
        "total_tokens_output": 0,
        "total_tokens_cache_creation": 0,
        "total_tokens_cache_read": 0,
        "cost_usd_estimate": 0.0,
        "cost_known_complete": 1,
        "first_active_at": None,
        "last_active_at": None,
        "days_active_30": 0,
        "days_active_90": 0,
        "days_active_lifespan": 0,
        "top_files_json": "[]",
        "models_used_json": "{}",
    }


def _days_active_within(days_seen: set[str]) -> tuple[int, int]:
    """Count distinct 'YYYY-MM-DD' strings within the last 30 / 90
    days (relative to UTC now)."""
    now = _utc_now()
    cutoff_30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_90 = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    n30 = sum(1 for d in days_seen if d >= cutoff_30)
    n90 = sum(1 for d in days_seen if d >= cutoff_90)
    return n30, n90


def _lifespan_days(earliest_iso, latest_iso) -> int:
    """Days between first and last activity (inclusive). 0 if either
    timestamp is missing or unparseable."""
    a = _parse_iso_safe(earliest_iso)
    b = _parse_iso_safe(latest_iso)
    if not a or not b or b < a:
        return 0
    return max(1, (b.date() - a.date()).days + 1)


# ── Public API: refresh ──────────────────────────────────────────


def refresh_summary(db, project: str) -> dict:
    """Recompute stats for one project and upsert to
    project_summaries. Returns the upserted dict."""
    stats = compute_project_stats(db, project)
    db.upsert_project_summary(project=project, **stats)
    return {"ok": True, "project": project, "stats": stats}


def refresh_all_summaries(db) -> dict:
    """Recompute stats for every distinct project in
    imported_sessions. Returns counts for the caller to log."""
    projects = db.list_distinct_imported_projects()
    refreshed = 0
    failed = 0
    errors: list[str] = []
    for project in projects:
        try:
            refresh_summary(db, project)
            refreshed += 1
        except Exception as e:
            failed += 1
            errors.append("{}: {}".format(project, e))
            log.exception("refresh_summary failed for %s: %s", project, e)
    return {
        "ok": True,
        "projects_total": len(projects),
        "refreshed": refreshed,
        "failed": failed,
        "errors": errors[:10],
    }
