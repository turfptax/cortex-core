"""Rules engine for the overseer's notifications.

Runs each tick after the main consolidation work. Each rule scans some
state (cortex.db read-only or overseer.db) and emits zero or more
notifications. Rules are deterministic and free — no LLM calls — so
running them every tick is essentially zero-cost.

Idempotency comes from the notifications table's
UNIQUE(rule_name, rule_key) constraint: emitting the same
(rule, key) is a no-op (or updates title/body if the underlying state
changed). User dismissal is sticky — `dismissed_at` doesn't get cleared
just because the rule fires again.

Adding a new rule = define a function that returns
list[dict(severity, title, body, related_table, related_id, action_url,
rule_name, rule_key)] and add it to RULES below.

For 3e ships: stale_active_project, automation_anomaly,
import_backlog. Rules around overdue reminders / pattern drift /
[low]-confidence interpretation review are deferred — the table + API
are ready for them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any


log = logging.getLogger("plugin.overseer.notifications")


# ── Helpers ─────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Tolerate both "2026-05-02 18:30:00" and "2026-05-02T18:30:00Z"
        s = s.replace("Z", "+00:00").replace("T", " ")
        # Strip a fractional-seconds suffix if present
        if "." in s:
            head, _, tail = s.partition(".")
            # Keep up to 6 digits then re-attach the timezone
            # (we're loose — these are best-effort parses)
            if "+" in tail or "-" in tail[1:]:
                # Find timezone offset boundary
                for i, ch in enumerate(tail):
                    if ch in "+-" and i > 0:
                        head = head + "." + tail[:i]
                        s = head + tail[i:]
                        break
            else:
                s = head + "." + tail[:6]
        # Try the pythonic parse
        if " " in s and "+" not in s and "-" not in s.replace("-0", "x", 1):
            # Naive — assume UTC
            d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            return d.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ── Rules ───────────────────────────────────────────────────────

def rule_stale_active_project(*, db, core_memory, config) -> list[dict]:
    """Active project not touched in N days → warn (or important after 30d)."""
    if core_memory is None or not getattr(core_memory, "is_open", False):
        return []
    threshold_days = int(config.get(
        "notify_stale_project_days", 14))
    important_days = int(config.get(
        "notify_stale_project_important_days", 30))
    rows = core_memory.query(
        "SELECT tag, name, last_touched FROM projects "
        "WHERE status = 'active' "
        "AND last_touched < datetime('now', ?)",
        ("-{} days".format(threshold_days),),
    )
    out = []
    now = _utc_now()
    for r in rows:
        last = _parse_iso(r.get("last_touched"))
        # Handle both naive and aware datetimes from _parse_iso (cortex.db
        # stores "YYYY-MM-DD HH:MM:SS" which parses naive on some paths)
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        days = (now - last).days if last else threshold_days
        severity = "important" if days >= important_days else "warn"
        out.append({
            "rule_name": "stale_active_project",
            "rule_key": r["tag"],
            "severity": severity,
            "title": "Stale: {}".format(r.get("name") or r["tag"]),
            "body": ("Project '{tag}' is marked active but was last "
                     "touched {d} days ago ({when}). Consider archiving, "
                     "marking dormant, or doing a small touch.").format(
                tag=r["tag"], d=days,
                when=(r.get("last_touched") or "?")[:10]),
            "related_table": "projects",
            "related_id": r["tag"],
            "action_url": "",
        })
    return out


def rule_automation_anomaly(*, db, core_memory, config) -> list[dict]:
    """Automation rollups with error_signals > 0 → warn."""
    rows = db._conn.execute(
        "SELECT id, project, rollup_date, session_count, error_signals, "
        "summary FROM automation_rollups "
        "WHERE error_signals > 0 ORDER BY rollup_date DESC LIMIT 50"
    ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        out.append({
            "rule_name": "automation_anomaly",
            "rule_key": "{}:{}".format(r["project"], r["rollup_date"]),
            "severity": "warn",
            "title": "{} ({}): {} runs with errors".format(
                r["project"], r["rollup_date"], r["error_signals"]),
            "body": (r.get("summary") or "")[:600],
            "related_table": "automation_rollups",
            "related_id": str(r["id"]),
            "action_url": "",
        })
    return out


def rule_import_backlog(*, db, core_memory, config) -> list[dict]:
    """Big backlog of unprocessed imports → info. Encourages a /backfill
    decision rather than letting the loop dribble through over weeks."""
    threshold = int(config.get("notify_backlog_imports", 50))
    total = db.imported_session_count()
    processed = db._conn.execute(
        "SELECT COUNT(*) FROM processed_imported_sessions"
    ).fetchone()[0]
    backlog = total - processed
    if backlog < threshold:
        return []
    return [{
        "rule_name": "import_backlog",
        "rule_key": "default",
        "severity": "info",
        "title": "{} imports waiting to be summarized".format(backlog),
        "body": ("{tot} total imports, {proc} summarized so far. The "
                 "loop processes up to 10 per tick (≈$0.50/tick). To "
                 "drain quickly, POST /plugins/overseer/backfill with "
                 "kind='imports' and a higher max_cost_usd.").format(
            tot=total, proc=processed),
        "related_table": "imported_sessions",
        "related_id": "",
        "action_url": "",
    }]


# ── Registry ────────────────────────────────────────────────────

RULES = [
    rule_stale_active_project,
    rule_automation_anomaly,
    rule_import_backlog,
]


def evaluate_rules(*, db, core_memory, config) -> dict:
    """Run all rules; emit notifications for the results.

    Polish CP2: also auto-archive stale notifications:
      - Per-rule: when a rule_key that previously fired no longer
        appears in this cycle's results, archive the existing
        notification (the underlying condition cleared)
      - Time-based: stale_active_project notifications older than
        notification_stale_archive_days (default 60) get auto-archived
        regardless of whether the project is still stale, because the
        signal stops being actionable after that long

    Returns a per-tick summary:
        {emitted, errors, by_rule, auto_resolved, auto_archived_stale}.
    """
    emitted = 0
    errors = 0
    by_rule: dict[str, int] = {}
    # Track which (rule_name, rule_key) pairs fired this cycle so we
    # can auto-resolve any prior notifications whose key dropped out.
    current_keys: dict[str, set[str]] = {}
    for rule in RULES:
        rule_name = rule.__name__.replace("rule_", "")
        current_keys.setdefault(rule_name, set())
        try:
            results = rule(db=db, core_memory=core_memory, config=config)
        except Exception as e:
            log.exception("rule %s failed: %s", rule_name, e)
            errors += 1
            continue
        for r in results or []:
            try:
                db.emit_notification(**r)
                emitted += 1
                by_rule[rule_name] = by_rule.get(rule_name, 0) + 1
                current_keys[rule_name].add(r.get("rule_key") or "")
            except Exception as e:
                log.warning("emit_notification failed for %s/%s: %s",
                            r.get("rule_name"), r.get("rule_key"), e)

    # Auto-resolve: keys that were active before but didn't fire now.
    auto_resolved = 0
    try:
        auto_resolved = db.auto_resolve_stale_rules(
            current_rule_keys=current_keys)
    except Exception as e:
        log.exception("auto_resolve_stale_rules failed: %s", e)

    # Time-based auto-archive for stale_active_project specifically.
    # Other rules get the auto-resolve treatment but no time limit;
    # stale_active_project specifically can sit there for months
    # because the user hasn't archived OR touched the project, and
    # at some point the noise-to-signal ratio flips.
    auto_archived_stale = 0
    try:
        days = int(config.get("notification_stale_archive_days", 60))
        auto_archived_stale = db.auto_archive_stale_notifications(
            rule_name="stale_active_project", older_than_days=days,
        )
    except Exception as e:
        log.exception("auto_archive_stale_notifications failed: %s", e)

    return {
        "emitted": emitted, "errors": errors, "by_rule": by_rule,
        "auto_resolved": auto_resolved,
        "auto_archived_stale": auto_archived_stale,
    }
