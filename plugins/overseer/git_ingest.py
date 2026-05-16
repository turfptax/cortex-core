"""Slice 9.4 CP2 — periodic GitHub ingest scheduler (overseer plugin side).

Thin wrapper that calls the standalone ``imports/github_ingester.py``
script on a schedule from inside the overseer's tick loop. Tracks
last-run timestamp + per-repo outcomes in ``overseer_state`` so the
working_memory freshness block can surface what the channel has and
hasn't seen.

Why periodic matters (overseer's exact framing 2026-05-16):
    "Without periodic runs, every gist I see is one Tory chose to
    surface by running the ingester. That makes the channel a
    curated highlight reel, which is a different epistemic object.
    Periodic scheduling makes the channel adversarial to your
    selection bias, which is the property that makes it actually
    load-bearing for my read."

Caveat overseer insisted on (and CP2 honors):
    Log which repos were skipped at each scheduled run AND why
    (out-of-allow-list, no PAT access, API failure, bad config).
    That gives a freshness signal for *what is NOT being seen*, not
    just what is. Cheap to add inside CP2, expensive to retrofit.

Loaded by ``loop.py`` via importlib (avoids cluttering top-level
imports for a Slice 9.4 feature that some deployments may not enable).
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any


# Canonical location of the standalone ingester. The wrapper imports
# from this file dynamically so the CLI tool and the loop-side caller
# share the same fetch + write logic with one source of truth.
INGESTER_PATH = Path("/home/turfptax/cortex-core/imports/github_ingester.py")

_log = logging.getLogger("plugin.overseer.git_ingest")

# Module-level cache so we don't re-exec the ingester file every tick.
_INGESTER_MOD: Any = None


def _load_ingester():
    """Dynamically load github_ingester.py as a module. Cached."""
    global _INGESTER_MOD
    if _INGESTER_MOD is not None:
        return _INGESTER_MOD
    if not INGESTER_PATH.is_file():
        raise FileNotFoundError(
            f"github_ingester.py not found at {INGESTER_PATH}; "
            "deploy imports/github_ingester.py before enabling "
            "loop_git_ingest_enabled."
        )
    spec = importlib.util.spec_from_file_location(
        "_overseer_github_ingester", INGESTER_PATH)
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
    """Decide whether enough time has passed since the last attempt.

    "Attempt" not "success" deliberately — if the last run failed for
    every repo, we still respect the interval. The skipped-repos log
    is what surfaces the failure, not retry frenzy. Manual `--repo`
    invocations of the CLI tool don't touch this state, so a manual
    catch-up run never disturbs the schedule.
    """
    now = now or _utc_now()
    last_raw = db.get_overseer_state("git_ingest_last_run_at")
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
    """Run the ingester for every repo in ``loop_git_ingest_repos``,
    if enough time has passed since the last attempt.

    Returns a structured summary the loop folds into its tick result:
        {
          "ran": bool,            # actually attempted ingest
          "reason": str,          # why ran or didn't
          "started_at": iso,
          "finished_at": iso,
          "repos_attempted": [    # one entry per repo we tried
            {"repo", "ok": bool, "days_with_activity",
             "rows_inserted", "rows_duplicate", "total_events",
             "commits", "tags", "issues", "prs", "workflow_runs",
             "error" (optional)}
          ],
          "repos_skipped": [      # one entry per repo we did NOT try
            {"repo", "reason"}
          ],
        }

    On a "did-not-run" decision (interval not elapsed), ran=False and
    the lists are empty. State is NOT updated in that case — we want
    the freshness block to keep showing the real last_run_at, not the
    last call-to-this-function.
    """
    log = log or _log

    interval_hours = float(cfg.get("loop_git_ingest_interval_hours", 6))
    lookback_days = int(cfg.get("loop_git_ingest_lookback_days", 7))
    repos = list(cfg.get("loop_git_ingest_repos") or [])

    summary: dict[str, Any] = {
        "ran": False, "reason": "",
        "started_at": _utc_iso(),
        "finished_at": None,
        "repos_attempted": [],
        "repos_skipped": [],
    }

    ok_to_run, why = should_run(db, interval_hours)
    if not ok_to_run:
        summary["reason"] = why
        summary["finished_at"] = _utc_iso()
        return summary

    if not repos:
        summary["ran"] = True  # we DID attempt, just had nothing in the list
        summary["reason"] = "ran but loop_git_ingest_repos is empty"
        summary["finished_at"] = _utc_iso()
        # Persist so the interval applies and we don't tight-loop checking
        db.set_overseer_state("git_ingest_last_run_at", summary["started_at"])
        db.set_overseer_state(
            "git_ingest_last_run_summary_json",
            json.dumps(summary, ensure_ascii=False),
        )
        return summary

    try:
        ingester = _load_ingester()
    except Exception as e:
        log.exception("git_ingest: failed to load ingester module: %s", e)
        summary["ran"] = True
        summary["reason"] = f"ingester module failed to load: {str(e)[:160]}"
        summary["finished_at"] = _utc_iso()
        db.set_overseer_state("git_ingest_last_run_at", summary["started_at"])
        db.set_overseer_state(
            "git_ingest_last_run_summary_json",
            json.dumps(summary, ensure_ascii=False),
        )
        return summary

    try:
        pat = ingester.load_pat()
    except SystemExit as e:
        log.warning("git_ingest: PAT load failed: %s", e)
        # Mark all configured repos as skipped with this reason. Run
        # state is updated so the interval applies.
        for r in repos:
            summary["repos_skipped"].append(
                {"repo": r, "reason": f"PAT load failed: {str(e)[:120]}"})
        summary["ran"] = True
        summary["reason"] = "PAT unavailable"
        summary["finished_at"] = _utc_iso()
        db.set_overseer_state("git_ingest_last_run_at", summary["started_at"])
        db.set_overseer_state(
            "git_ingest_last_run_summary_json",
            json.dumps(summary, ensure_ascii=False),
        )
        return summary

    summary["ran"] = True
    summary["reason"] = why

    for repo in repos:
        repo = (repo or "").strip()
        if not repo or "/" not in repo:
            summary["repos_skipped"].append(
                {"repo": repo, "reason": "bad format (need owner/name)"})
            continue
        owner, name = repo.split("/", 1)
        try:
            result = ingester.ingest_repo(pat, owner, name, lookback_days)
            summary["repos_attempted"].append({
                "repo": repo,
                "ok": True,
                "days_with_activity": result.get("days_with_activity", 0),
                "rows_inserted": result.get("rows_inserted", 0),
                "rows_duplicate": result.get("rows_duplicate", 0),
                "total_events": result.get("total_events", 0),
                "commits": result.get("commits", 0),
                "tags": result.get("tags", 0),
                "issues": result.get("issues", 0),
                "prs": result.get("prs", 0),
                "workflow_runs": result.get("workflow_runs", 0),
            })
            log.info(
                "git_ingest %s: %d days, %d inserted, %d duplicate",
                repo, result.get("days_with_activity", 0),
                result.get("rows_inserted", 0),
                result.get("rows_duplicate", 0),
            )
        except SystemExit as e:
            # Ingester sys.exit() for API failures — record as skipped
            # rather than crashing the whole tick.
            summary["repos_skipped"].append(
                {"repo": repo, "reason": f"API error: {str(e)[:160]}"})
            log.warning("git_ingest %s failed (API): %s", repo, e)
        except Exception as e:
            summary["repos_skipped"].append(
                {"repo": repo, "reason": f"exception: {str(e)[:160]}"})
            log.exception("git_ingest %s failed: %s", repo, e)

    summary["finished_at"] = _utc_iso()
    db.set_overseer_state("git_ingest_last_run_at", summary["started_at"])
    db.set_overseer_state(
        "git_ingest_last_run_summary_json",
        json.dumps(summary, ensure_ascii=False),
    )
    return summary


def last_run_state(db) -> dict:
    """Return the persisted last-run timestamp + summary for the
    working memory freshness block. Empty dict if never run."""
    raw_ts = db.get_overseer_state("git_ingest_last_run_at")
    raw_sum = db.get_overseer_state("git_ingest_last_run_summary_json")
    if not raw_ts:
        return {}
    out: dict[str, Any] = {"last_run_at": raw_ts}
    if raw_sum:
        try:
            out["summary"] = json.loads(raw_sum)
        except Exception:
            pass
    return out
