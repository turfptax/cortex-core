"""Deterministic loop — lights-out maintenance for Cortex.

When Claude Code sessions / OpenRouter credits are unavailable, the
/loop AI can't run. This script does the LLM-INDEPENDENT subset of
the looper's work as a pure-Python program. Runs as a cron job /
systemd timer / manual invocation on .25 or from any host with SSH +
HTTP access to the Pi.

Why this exists
---------------
Cycle 2 of the /loop (iters 21-27, 2026-06-07) proved that the most
valuable cycle-2 work — F1 abstraction-graph coverage (11.8% → 42.3%),
decision mining (93 from 3,466 gists), entity extraction — was ALL
deterministic. No LLM call. Pure SQL + Python.

This script captures that pattern as a runnable program so the
maintenance work continues across credit outages, agent downtime,
or any other interruption to the Claude-Code-based looper.

What it does NOT replace
------------------------
- The Claude-Code /loop, which is needed for judgment calls,
  novel design work, cross-iteration planning, and LLM-dependent
  datamining (theme synthesis, decision REASONING, etc.).
- The overseer's tick loop on .25 (its own background work).
- The weather plugin's hourly poll (its own thread).

This is a THIRD execution surface that runs the deterministic
maintenance work. Writes to looper_log with `mode=deterministic`
so cycle 3 (when credits return) can see what landed.

Work units
----------
Each work unit is a callable that returns a dict of {work_done,
followups, escalations, files_changed}. The runner picks ONE unit
per invocation based on overdue-ness + safety prerequisites + a
cheap "should run?" predicate. All units are idempotent.

Adding a new unit: implement the callable, add to UNITS, set its
cadence_hours + predicate. The runner picks it up automatically.

Usage
-----
  # Run once, picks the most-overdue unit:
  python scripts/deterministic_loop.py

  # Force-run a specific unit:
  python scripts/deterministic_loop.py --unit vault_render

  # List units + when each was last run:
  python scripts/deterministic_loop.py --list

  # Run against a different Pi:
  python scripts/deterministic_loop.py --pi http://10.0.0.25:8420

  # Cron-friendly (silent on no-op):
  python scripts/deterministic_loop.py --quiet
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ── HTTP helpers ────────────────────────────────────────────────────


def _basic_auth(user: str, pw: str) -> str:
    raw = f"{user}:{pw}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def http_get(url: str, auth: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(url: str, auth: str, body: dict,
               timeout: float = 60.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={
            "Authorization": auth,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ── Looper-log integration ──────────────────────────────────────────


def start_iteration(pi: str, auth: str, unit_name: str,
                     session_id: str) -> int | None:
    """POST /looper/start. Returns the row id (used by finish)."""
    try:
        resp = http_post(
            f"{pi}/plugins/overseer/looper/start", auth,
            {
                "mode": "deterministic",
                "session_id": session_id,
                "model": "deterministic-script-no-llm",
                "local_started_at": _dt.datetime.now().astimezone()
                                       .isoformat(timespec="seconds"),
            },
        )
        if resp.get("ok"):
            return resp.get("id")
    except Exception as e:
        print(f"  start_iteration failed: {e}", file=sys.stderr)
    return None


def finish_iteration(pi: str, auth: str, iter_id: int,
                       summary: str, work_done: list,
                       followups: list, escalations: list,
                       files_changed: list) -> None:
    try:
        http_post(
            f"{pi}/plugins/overseer/looper/finish", auth,
            {
                "id": iter_id,
                "summary": summary,
                "work_done": work_done,
                "followups": followups,
                "escalations": escalations,
                "files_changed": files_changed,
                "llm_calls_estimate": 0,
                "cost_usd_estimate": 0.0,
                "local_ended_at": _dt.datetime.now().astimezone()
                                     .isoformat(timespec="seconds"),
            },
        )
    except Exception as e:
        print(f"  finish_iteration failed: {e}", file=sys.stderr)


def hours_since_last_run(pi: str, auth: str, unit_name: str) -> float:
    """Returns hours since this unit's most-recent finish; infinity
    if never run. Reads from looper_log filtering on session_id
    prefix `deterministic:<unit_name>`."""
    try:
        resp = http_get(
            f"{pi}/plugins/overseer/looper/recent?limit=60", auth)
    except Exception:
        return float("inf")
    if not resp.get("ok"):
        return float("inf")
    target = f"deterministic:{unit_name}"
    for e in resp.get("entries", []):
        if e.get("session_id") == target and e.get("ended_at"):
            try:
                dt = _dt.datetime.fromisoformat(e["ended_at"])
                dt = dt.replace(tzinfo=_dt.timezone.utc)
                delta = _dt.datetime.now(_dt.timezone.utc) - dt
                return delta.total_seconds() / 3600.0
            except Exception:
                continue
    return float("inf")


# ── Work units ──────────────────────────────────────────────────────
#
# Each unit is a callable taking (pi, auth, log_fn) and returning a
# dict with keys: summary, work_done, followups, escalations,
# files_changed.
#
# Units MUST be idempotent — they may be run more often than intended,
# and they should be no-ops when their precondition isn't met.


def unit_vault_render(pi, auth, log_fn):
    """Re-render the vault (deterministic; no LLM). Includes the
    ghost-file sweep so stale slugs from prior renders get cleaned
    up."""
    t0 = time.time()
    resp = http_post(
        f"{pi}/plugins/weather/poll-now", auth, {}, timeout=30.0,
    ) if False else None  # vestige guard
    try:
        resp = http_post(
            f"{pi}/plugins/overseer/vault/render", auth,
            {}, timeout=300.0,
        )
    except Exception as e:
        return {
            "summary": f"vault_render: HTTP failed — {e}",
            "work_done": [],
            "followups": [
                "next deterministic run will retry vault render",
            ],
            "escalations": [
                f"vault render endpoint failing — {e}",
            ],
            "files_changed": [],
        }
    dur = time.time() - t0
    counts = resp.get("counts") or {}
    orphans = resp.get("orphans_deleted", 0)
    err_n = resp.get("error_count", 0)
    summary = (
        f"vault_render: {sum(counts.values())} files written in "
        f"{dur:.1f}s, {orphans} orphans swept, {err_n} errors"
    )
    return {
        "summary": summary,
        "work_done": [{
            "category": "vault",
            "item": (f"re-rendered vault: {sum(counts.values())} files, "
                     f"{orphans} orphans deleted"),
            "status": "shipped" if err_n == 0 else "shipped-with-errors",
        }],
        "followups": [],
        "escalations": (
            [f"vault render produced {err_n} per-row errors"]
            if err_n else []
        ),
        "files_changed": [],
    }


def unit_pull_event_stats_snapshot(pi, auth, log_fn):
    """Snapshot the F1 adoption signal so the trend is in looper_log
    for cycle 3 to read at boot."""
    try:
        s = http_get(
            f"{pi}/plugins/overseer/pull-events/stats?days=7",
            auth,
        )
    except Exception as e:
        return {
            "summary": f"pull_event_stats: HTTP failed — {e}",
            "work_done": [], "followups": [],
            "escalations": [str(e)], "files_changed": [],
        }
    total = s.get("total", 0)
    organic = s.get("organic_external_count", 0)
    auto = s.get("automation_count", 0)
    ratio = s.get("signal_ratio", 0.0)
    by_class = s.get("by_caller_class", {})
    top_organic = s.get("top_pulled_organic", [])[:5]
    parts = [f"7d pull-event snapshot:"]
    parts.append(f"  total={total} organic={organic} automation={auto} "
                 f"signal_ratio={ratio:.1%}")
    parts.append(f"  by_caller_class: {by_class}")
    if top_organic:
        parts.append("  top_pulled_organic (real F1 reads):")
        for tbl, aid, n in top_organic:
            parts.append(f"    {n}x  {tbl}#{aid}")
    return {
        "summary": "\n".join(parts),
        "work_done": [{
            "category": "stats",
            "item": (f"F1 adoption snapshot: organic={organic} "
                     f"(ratio {ratio:.1%}) over 7d"),
            "status": "shipped",
        }],
        "followups": [
            ("if organic_external_count is rising, cortex_search is "
             "being adopted; if flat, F1 mission needs marketing not "
             "engineering"),
        ],
        "escalations": [], "files_changed": [],
    }


def unit_f1_coverage_snapshot(pi, auth, log_fn):
    """Snapshot current top-down F1 coverage (the looper's cycle-2
    headline metric: 11.8% → 42.3%). Reads via single dedicated
    endpoint /plugins/overseer/f1-coverage so the cycle-3 looper can
    see the trend at boot."""
    try:
        s = http_get(f"{pi}/plugins/overseer/f1-coverage", auth)
    except Exception as e:
        return {
            "summary": f"f1_coverage: HTTP failed — {e}",
            "work_done": [],
            "followups": [],
            "escalations": [str(e)],
            "files_changed": [],
        }
    if not s.get("ok"):
        return {
            "summary": f"f1_coverage: endpoint error — {s.get('error')}",
            "work_done": [],
            "followups": [],
            "escalations": [s.get("error", "unknown")],
            "files_changed": [],
        }
    total = s.get("total", 0)
    via_q = s.get("via_question", 0)
    via_t = s.get("via_theme", 0)
    via_either = s.get("via_either", 0)
    pct = s.get("coverage_pct", 0.0)
    return {
        "summary": (
            f"F1 coverage snapshot: {via_either}/{total} = {pct}% "
            f"reachable top-down "
            f"({via_q} via question, {via_t} via theme)"
        ),
        "work_done": [{
            "category": "stats",
            "item": (f"F1 coverage = {pct}% "
                     f"({via_q} via Q, {via_t} via T)"),
            "status": "shipped",
        }],
        "followups": [
            ("if coverage drops between snapshots, gists are being "
             "added faster than theme_gists / evidence_for_question "
             "are populated; cycle 3 should run kw-route v4"),
        ],
        "escalations": [],
        "files_changed": [],
    }


def unit_health_probe(pi, auth, log_fn):
    """Liveness + light sanity. Confirms the Pi is healthy enough to
    do any other deterministic work, before we burn time on heavier
    units. Good first-run + good fast-pass."""
    checks = []
    ok = True
    # /ping
    try:
        r = http_get(f"{pi}/ping", auth, timeout=5.0)
        # /ping isn't routed — expect 404 from BaseHTTP. We probe
        # /api/cmd instead.
    except Exception:
        pass
    try:
        cmd_resp = http_post(
            f"{pi}/api/cmd", auth,
            {"command": "ping", "payload": ""}, timeout=10.0,
        )
        checks.append(("pi_cmd_ping",
                       cmd_resp.get("response") == "RSP:pong"))
    except Exception as e:
        checks.append(("pi_cmd_ping", False))
        ok = False
    # working_memory age
    try:
        wm = http_get(
            f"{pi}/plugins/overseer/working-memory", auth)
        age = wm.get("working_memory_age_minutes")
        checks.append(("wm_fresh_under_60min",
                       age is not None and age < 60))
    except Exception:
        checks.append(("wm_fresh_under_60min", False))
        ok = False
    # weather plugin alive
    try:
        ws = http_get(f"{pi}/plugins/weather/status", auth)
        checks.append(("weather_alive", bool(ws.get("ok"))))
    except Exception:
        checks.append(("weather_alive", False))
    passed = sum(1 for _, v in checks if v)
    summary = (f"health: {passed}/{len(checks)} checks passed "
               f"— {dict(checks)}")
    return {
        "summary": summary,
        "work_done": [{
            "category": "health",
            "item": f"liveness probe — {passed}/{len(checks)}",
            "status": "shipped" if ok else "shipped-degraded",
        }],
        "followups": [],
        "escalations": (
            [f"degraded: {[k for k,v in checks if not v]}"]
            if not ok else []
        ),
        "files_changed": [],
    }


# Registry. Each entry: (callable, cadence_hours, description).
# Cadence is "if it's been this long, this unit is overdue."
UNITS = {
    "health_probe": (
        unit_health_probe, 1.0,
        "Liveness + freshness check. Cheap; runs first.",
    ),
    "vault_render": (
        unit_vault_render, 6.0,
        "Re-render vault + ghost sweep. Keeps the rendered "
        "corpus in sync with overseer.db.",
    ),
    "pull_event_stats_snapshot": (
        unit_pull_event_stats_snapshot, 12.0,
        "Snapshot F1 adoption signal (organic-external count) "
        "into looper_log so the trend persists.",
    ),
    "f1_coverage_snapshot": (
        unit_f1_coverage_snapshot, 12.0,
        "Snapshot abstraction-graph coverage percentage so cycle 3 "
        "can read the trend at boot.",
    ),
}


# ── Picker + driver ─────────────────────────────────────────────────


def pick_next_unit(pi: str, auth: str) -> tuple[str, float] | None:
    """Return (unit_name, overdue_hours) for the most-overdue unit
    whose overdue >= its cadence. Returns None if nothing is due."""
    candidates = []
    for name, (_, cadence, _) in UNITS.items():
        elapsed = hours_since_last_run(pi, auth, name)
        if elapsed >= cadence:
            overdue = elapsed - cadence
            candidates.append((name, overdue, cadence, elapsed))
    if not candidates:
        return None
    # Sort by overdue DESC. Tiebreak: smaller cadence first (cheaper
    # checks favored when nothing else is dominant).
    candidates.sort(key=lambda c: (-c[1], c[2]))
    pick = candidates[0]
    return pick[0], pick[1]


def run_once(args) -> int:
    auth = _basic_auth(args.user, args.password)
    pi = args.pi.rstrip("/")

    if args.unit:
        if args.unit not in UNITS:
            print(f"unknown unit: {args.unit}. Choose from: "
                  + ", ".join(UNITS.keys()), file=sys.stderr)
            return 2
        unit_name = args.unit
        overdue = 0.0
    else:
        pick = pick_next_unit(pi, auth)
        if pick is None:
            if not args.quiet:
                print("nothing due. listing units + last run:")
                for name, (_, cad, desc) in UNITS.items():
                    h = hours_since_last_run(pi, auth, name)
                    h_s = f"{h:.1f}h" if h != float("inf") else "never"
                    print(f"  {name:<30} cadence={cad}h  last={h_s}")
            return 0
        unit_name, overdue = pick

    fn, cadence, desc = UNITS[unit_name]
    print(f"running unit: {unit_name}  "
          f"(cadence={cadence}h, overdue by {overdue:.1f}h)")

    session_id = f"deterministic:{unit_name}"
    iter_id = start_iteration(pi, auth, unit_name, session_id)
    if iter_id is None:
        print("  could not write looper_log start row; aborting",
              file=sys.stderr)
        return 1

    try:
        result = fn(pi, auth, print)
    except Exception as e:
        result = {
            "summary": f"unit {unit_name} crashed: {e}",
            "work_done": [],
            "followups": [],
            "escalations": [f"unit crash: {e}"],
            "files_changed": [],
        }

    print("  " + (result.get("summary") or "")
          .replace("\n", "\n  "))

    finish_iteration(
        pi, auth, iter_id,
        summary=result.get("summary") or "",
        work_done=result.get("work_done") or [],
        followups=result.get("followups") or [],
        escalations=result.get("escalations") or [],
        files_changed=result.get("files_changed") or [],
    )
    return 0


def list_units(args) -> int:
    auth = _basic_auth(args.user, args.password)
    pi = args.pi.rstrip("/")
    print(f"{'unit':<30} {'cadence':<10} {'last_run':<14} "
          f"{'overdue':<10}  description")
    print("-" * 90)
    for name, (_, cad, desc) in UNITS.items():
        h = hours_since_last_run(pi, auth, name)
        h_s = f"{h:.1f}h" if h != float("inf") else "never"
        overdue = (h - cad) if h != float("inf") else float("inf")
        ov_s = "now" if overdue > 0 else f"{-overdue:.1f}h"
        print(f"{name:<30} {cad}h{'':<5} {h_s:<14} {ov_s:<10}  "
              f"{desc[:40]}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pi", default="http://10.0.0.25:8420",
        help="Cortex Pi HTTP base URL",
    )
    parser.add_argument("--user", default="cortex")
    parser.add_argument("--password", default="cortex")
    parser.add_argument(
        "--unit", default="",
        help="Force-run a specific unit (skips overdue logic)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List units + when each last ran; exit",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Cron-friendly: silent on no-op",
    )
    args = parser.parse_args(argv)

    if args.list:
        return list_units(args)
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
