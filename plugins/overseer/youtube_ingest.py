"""Periodic YouTube persona-channel ingest (overseer plugin side).

Thin wrapper that calls the standalone ``imports/youtube_ingester.py``
on a schedule from inside the overseer's tick loop, exactly like
``git_ingest.py`` wraps the GitHub ingester. Tracks last-run timestamp
plus per-channel outcomes in ``overseer_state`` so the working_memory
freshness block can surface what the persona channel has and hasn't
seen.

Why this channel exists (persona-tracking direction, 2026-06-11):
the git channel catches what Tory SHIPS, the chat channels catch what
he THINKS, this catches what his public personas SAY (TURFPTAx,
DuelingGroks, OpenMuscle). Public posts are also the lowest-sensitivity
rows in the corpus, which makes them the safest material for the
Gateway parity push later.

Loaded by ``loop.py`` via importlib, same as git_ingest.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any


INGESTER_PATH = Path(
    "/home/turfptax/cortex-core/imports/youtube_ingester.py")

_log = logging.getLogger("plugin.overseer.youtube_ingest")

_INGESTER_MOD: Any = None


def _load_ingester():
    """Dynamically load youtube_ingester.py as a module. Cached."""
    global _INGESTER_MOD
    if _INGESTER_MOD is not None:
        return _INGESTER_MOD
    if not INGESTER_PATH.is_file():
        raise FileNotFoundError(
            f"youtube_ingester.py not found at {INGESTER_PATH}; "
            "deploy imports/youtube_ingester.py before enabling "
            "loop_youtube_ingest_enabled.")
    spec = importlib.util.spec_from_file_location(
        "_overseer_youtube_ingester", INGESTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _INGESTER_MOD = mod
    return mod


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _utc_iso(dt: datetime.datetime | None = None) -> str:
    return (dt or _utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def should_run(db, interval_hours: float,
               now: datetime.datetime | None = None
               ) -> tuple[bool, str]:
    """Same attempt-not-success interval contract as git_ingest:
    a failed run still respects the interval; the skipped-channels log
    surfaces the failure, not retry frenzy. Manual CLI runs don't touch
    this state, so a manual backfill never disturbs the schedule."""
    now = now or _utc_now()
    last_raw = db.get_overseer_state("youtube_ingest_last_run_at")
    if not last_raw:
        return True, "first run"
    last = _parse_iso(last_raw)
    if last is None:
        return True, f"unparseable last_run_at: {last_raw!r}"
    elapsed_h = (now - last).total_seconds() / 3600.0
    if elapsed_h < interval_hours:
        return False, (f"only {elapsed_h:.1f}h since last run "
                       f"(interval {interval_hours}h)")
    return True, f"{elapsed_h:.1f}h since last run"


def run_scheduled(db, cfg, log: logging.Logger | None = None) -> dict:
    """Ingest every channel in ``loop_youtube_channels`` if enough time
    has passed. Returns a structured summary the loop folds into its
    tick result:
        {
          "ran": bool, "reason": str,
          "started_at": iso, "finished_at": iso,
          "channels_attempted": [
            {"persona", "channel_id", "project", "ok": bool,
             "videos_seen", "rows_inserted", "rows_duplicate",
             "rows_updated", "new_videos": [{"id", "title"}]}
          ],
          "channels_skipped": [{"channel", "reason"}],
        }
    """
    log = log or _log

    interval_hours = float(cfg.get(
        "loop_youtube_ingest_interval_hours", 12))
    channels = list(cfg.get("loop_youtube_channels") or [])

    summary: dict[str, Any] = {
        "ran": False, "reason": "",
        "started_at": _utc_iso(),
        "finished_at": None,
        "channels_attempted": [],
        "channels_skipped": [],
    }

    ok_to_run, why = should_run(db, interval_hours)
    if not ok_to_run:
        summary["reason"] = why
        summary["finished_at"] = _utc_iso()
        return summary

    def _persist():
        summary["finished_at"] = _utc_iso()
        db.set_overseer_state(
            "youtube_ingest_last_run_at", summary["started_at"])
        db.set_overseer_state(
            "youtube_ingest_last_run_summary_json",
            json.dumps(summary, ensure_ascii=False))

    if not channels:
        summary["ran"] = True
        summary["reason"] = "ran but loop_youtube_channels is empty"
        _persist()
        return summary

    try:
        ingester = _load_ingester()
    except Exception as e:
        log.exception("youtube_ingest: ingester load failed: %s", e)
        summary["ran"] = True
        summary["reason"] = f"ingester module failed to load: {str(e)[:160]}"
        _persist()
        return summary

    summary["ran"] = True
    summary["reason"] = why

    for spec in channels:
        try:
            persona, channel_id, project = ingester.parse_channel_spec(spec)
        except ValueError as e:
            summary["channels_skipped"].append(
                {"channel": str(spec), "reason": str(e)[:160]})
            continue
        try:
            result = ingester.ingest_channel(channel_id, persona, project)
            summary["channels_attempted"].append({
                "persona": persona,
                "channel_id": channel_id,
                "project": project,
                "ok": True,
                "videos_seen": result.get("videos_seen", 0),
                "rows_inserted": result.get("rows_inserted", 0),
                "rows_duplicate": result.get("rows_duplicate", 0),
                "rows_updated": result.get("rows_updated", 0),
                "new_videos": result.get("new_videos") or [],
            })
            log.info(
                "youtube_ingest %s: %d seen, %d inserted, %d duplicate",
                persona, result.get("videos_seen", 0),
                result.get("rows_inserted", 0),
                result.get("rows_duplicate", 0))
        except Exception as e:
            summary["channels_skipped"].append(
                {"channel": str(spec), "reason": f"exception: {str(e)[:160]}"})
            log.exception("youtube_ingest %s failed: %s", spec, e)

    _persist()
    return summary


def last_run_state(db) -> dict:
    """Persisted last-run timestamp + summary for the working memory
    freshness block. Empty dict if never run."""
    raw_ts = db.get_overseer_state("youtube_ingest_last_run_at")
    raw_sum = db.get_overseer_state(
        "youtube_ingest_last_run_summary_json")
    if not raw_ts:
        return {}
    out: dict[str, Any] = {"last_run_at": raw_ts}
    if raw_sum:
        try:
            out["summary"] = json.loads(raw_sum)
        except Exception:
            pass
    return out
