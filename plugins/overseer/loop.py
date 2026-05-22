"""OverseerLoop — background consolidation thread.

Heartbeat-pattern (proven from pet plugin's Heartbeat). Runs inside the
overseer plugin process; started in on_load, stopped in on_unload.

Per-tick work, in priority order (locked design 2026-05-02):
  1. Auto-summarize sessions that ended-but-not-yet-processed
  2. Auto-tag notes that have no tags AND haven't been processed
  3. Rebuild the working_memory artifact and cache it in overseer_state

Cost guards (in plugin.toml [config]):
  - loop_max_llm_calls_per_tick   default 10
  - loop_max_cost_usd_per_tick    default 0.50
  - loop_first_tick_delay_s       default 30 (don't hammer LLM at startup)

Idempotency:
  - processed_sessions and processed_notes tables in overseer.db
  - Safe to clear either; the next tick re-processes everything (matches
    overseer.db's drop-and-rebuild design)

Model selection:
  - Summarization → Opus 4.7 (high-stakes interpretive work)
  - Tagging      → Sonnet 4.6 (small structured task; ~5x cheaper)
  - LLMRouter resolves these via [llm.model_overrides] keyed on `purpose`

Concurrency:
  - Background thread + HTTP /tick-now serialize via _tick_lock so we
    can't double-process a session if the user hits /tick-now while the
    loop is mid-tick.
  - SQLite (CortexDB family) uses check_same_thread=False; reads are
    safe, writes serialize at the SQLite level. Per-call llm_calls
    logging is fine concurrent.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from claude_jsonl import (
    build_transcript_for_summary,
    parse_claude_code_jsonl,
)
from automation_rollup import generate_rollup
from notifications import evaluate_rules
from dialectic import paired_generate, write_dialectic_row
from prompts import (session_gist_prompt, import_gist_prompt,
                     import_gist_prompt_sanitized)
from journal import write_tick_journal_entry
from question_routing import route_evidence_to_questions
from blindspots import applicable_blindspots
from detail import make_token
from insight_scan import scan_project_arcs, select_eligible_projects
from distill_corrections import distill_uncondidated_corrections
import project_summary
import project_narrative
import temporal as T_clock
import temporal_narrative


log = logging.getLogger("plugin.overseer.loop")


# ── TickBudget ──────────────────────────────────────────────────

class TickBudget:
    """Per-tick LLM call + cost cap. Mutable; charged by the loop.

    Slice 3e: composed with a DailyBudget so every charge counts against
    BOTH limits. exhausted() is true when EITHER cap is hit.
    """

    def __init__(self, *, max_calls: int, max_cost_usd: float,
                 daily_budget=None):
        self.max_calls = int(max_calls)
        self.max_cost_usd = float(max_cost_usd)
        self.calls_used = 0
        self.cost_used = 0.0
        self._daily = daily_budget   # DailyBudget or None

    def charge(self, llm_result: dict) -> None:
        self.calls_used += 1
        cost = float(llm_result.get("cost_usd") or 0.0)
        self.cost_used += cost
        if self._daily is not None:
            self._daily.charge(cost=cost)

    def exhausted(self) -> bool:
        if (self.calls_used >= self.max_calls
                or self.cost_used >= self.max_cost_usd):
            return True
        if self._daily is not None and self._daily.exhausted():
            return True
        return False

    def remaining(self) -> dict:
        out = {
            "calls_remaining": max(0, self.max_calls - self.calls_used),
            "cost_remaining_usd": round(
                max(0.0, self.max_cost_usd - self.cost_used), 4),
            "calls_used": self.calls_used,
            "cost_used_usd": round(self.cost_used, 4),
        }
        if self._daily is not None:
            out["daily"] = self._daily.snapshot()
        return out


class DailyBudget:
    """LOCAL-day rolling cap on LLM spend.

    Slice 5.5 (cadence calibration): switched from UTC-day to
    local-day reset. The previous UTC reset at 00:00 UTC = 19:00
    CDT meant evening ticks (19:00-23:59 CDT) burned through the
    same budget pool that the next morning's ticks would also draw
    from — and the temporal-cadence step at 22:00 CDT was last in
    line, so it kept finding the budget exhausted by the time it
    fired. Local-day reset aligns the budget calendar with the
    user's calendar day AND the temporal-narrative period system,
    which already uses local dates.

    State persists in overseer_state under keys:
      - overseer_today_date            (YYYY-MM-DD LOCAL)
      - overseer_today_cost_usd        (float)
      - overseer_today_calls           (int)

    On each charge, if today's date != stored date, counters reset to
    zero. exhausted() is true if either cost or call cap is reached.

    Manual /backfill bypasses the daily budget by passing
    daily_budget=None into TickBudget — that's the explicit
    user-driven escape hatch.
    """

    KEY_DATE = "overseer_today_date"
    KEY_COST = "overseer_today_cost_usd"
    KEY_CALLS = "overseer_today_calls"

    def __init__(self, *, db, max_cost_usd: float, max_calls: int):
        self._db = db
        self.max_cost_usd = float(max_cost_usd)
        self.max_calls = int(max_calls)
        self._date, self._cost, self._calls = self._load()

    def _today(self) -> str:
        # Local date — uses host's TZ. Matches temporal_narratives
        # period_label semantics so budget rolls with the user's
        # calendar, not UTC's.
        return datetime.now().astimezone().strftime("%Y-%m-%d")

    def _load(self) -> tuple[str, float, int]:
        today = self._today()
        raw_stored = self._db.get_overseer_state(self.KEY_DATE)
        # If the date is unset (fresh DB) OR has rolled over, persist
        # today and zero counters. Both branches must write KEY_DATE so
        # subsequent reads can detect the next rollover correctly.
        if raw_stored is None or raw_stored != today:
            self._db.set_overseer_state(self.KEY_DATE, today)
            self._db.set_overseer_state(self.KEY_COST, "0")
            self._db.set_overseer_state(self.KEY_CALLS, "0")
            return today, 0.0, 0
        try:
            cost = float(self._db.get_overseer_state(self.KEY_COST) or 0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            calls = int(float(
                self._db.get_overseer_state(self.KEY_CALLS) or 0))
        except (TypeError, ValueError):
            calls = 0
        return today, cost, calls

    def _refresh_date(self) -> None:
        """Check if the local date rolled while we were running."""
        today = self._today()
        if today != self._date:
            self._date = today
            self._cost = 0.0
            self._calls = 0
            self._db.set_overseer_state(self.KEY_DATE, today)
            self._db.set_overseer_state(self.KEY_COST, "0")
            self._db.set_overseer_state(self.KEY_CALLS, "0")

    def charge(self, *, cost: float) -> None:
        self._refresh_date()
        self._cost += float(cost or 0.0)
        self._calls += 1
        self._db.set_overseer_state(self.KEY_COST, str(round(self._cost, 6)))
        self._db.set_overseer_state(self.KEY_CALLS, str(self._calls))

    def exhausted(self) -> bool:
        self._refresh_date()
        if self._cost >= self.max_cost_usd:
            return True
        if self._calls >= self.max_calls:
            return True
        return False

    def snapshot(self) -> dict:
        self._refresh_date()
        return {
            "date": self._date,
            "cost_used_usd": round(self._cost, 4),
            "cost_max_usd": self.max_cost_usd,
            "cost_remaining_usd": round(
                max(0.0, self.max_cost_usd - self._cost), 4),
            "calls_used": self._calls,
            "calls_max": self.max_calls,
            "calls_remaining": max(0, self.max_calls - self._calls),
            "exhausted": self.exhausted(),
        }


# ── Tag-line parser (cheap-model output) ────────────────────────

_TAG_LINE_RX = re.compile(r"^\s*(\d+)[\.\)]?\s+(.+?)\s*$")


def parse_tag_lines(text: str, expected_count: int,
                    *, max_per_note: int = 3) -> list[list[str]]:
    """Parse cheap-model batch tag output into per-note tag lists.

    Expects lines like:
      1. topic:llm, project:cortex
      2. theme:hardware
      3. (none)
    Tolerates blank lines, prose preamble, and missing entries (returns
    empty list for any unaddressed slot).
    """
    out: list[list[str]] = [[] for _ in range(expected_count)]
    for line in (text or "").splitlines():
        m = _TAG_LINE_RX.match(line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if not (0 <= idx < expected_count):
            continue
        body = m.group(2).strip()
        if body.lower() in ("(none)", "none", "-", "n/a", ""):
            continue
        # Split on commas; require namespaced "ns:slug" form to filter
        # out hallucinated prose. Keep at most max_per_note tags.
        candidates = [t.strip().strip(".") for t in body.split(",")]
        kept = [c for c in candidates if c and ":" in c and len(c) < 60]
        out[idx] = kept[:max_per_note]
    return out


# ── OverseerLoop ────────────────────────────────────────────────

class OverseerLoop:
    """Background consolidation worker.

    Owns its own thread; does not block plugin load. All work is gated
    on a TickBudget so a runaway LLM doesn't burn credits.
    """

    def __init__(self, *, db, llm, core_memory, config, log):
        self._db = db
        self._llm = llm
        self._core = core_memory
        self._cfg = config            # PluginConfig — reads plugin.toml [config]
        self._log = log
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self._stats: dict[str, Any] = {
            "started_at": None,
            "ticks_run": 0,
            "ticks_failed": 0,
            "last_tick_at": None,
            "last_tick_summary": None,
            "last_error": "",
        }

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the background thread. Returns False if disabled in config."""
        if not self._cfg.get("loop_enabled", True):
            self._log.info("loop disabled in config; not starting")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._stats["started_at"] = _utc_iso()
        self._thread = threading.Thread(
            target=self._run, name="overseer-loop", daemon=True,
        )
        self._thread.start()
        self._log.info(
            "loop started (tick=%ss, first_delay=%ss, "
            "max_calls/tick=%s, max_cost/tick=$%s)",
            self._cfg.get("tick_interval_s", 300),
            self._cfg.get("loop_first_tick_delay_s", 30),
            self._cfg.get("loop_max_llm_calls_per_tick", 10),
            self._cfg.get("loop_max_cost_usd_per_tick", 0.50),
        )
        return True

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> dict:
        s = dict(self._stats)
        s["running"] = self.is_running()
        return s

    # ── Main loop ────────────────────────────────────────────────

    def _run(self) -> None:
        # First-tick delay so we don't hammer the LLM at boot.
        delay = float(self._cfg.get("loop_first_tick_delay_s", 30))
        if self._stop.wait(timeout=delay):
            return
        interval = float(self._cfg.get("tick_interval_s", 300))
        while not self._stop.is_set():
            try:
                self.run_one_tick(trigger="scheduled")
            except Exception as e:
                self._stats["ticks_failed"] += 1
                self._stats["last_error"] = str(e)[:500]
                self._log.exception("tick failed: %s", e)
            if self._stop.wait(timeout=interval):
                return

    # ── One tick ─────────────────────────────────────────────────

    def run_one_tick(self, *, trigger: str = "scheduled") -> dict:
        """Execute one tick. Used by the background thread AND /tick-now.

        Serializes via _tick_lock so concurrent calls can't double-process.
        Returns the per-tick summary dict; also writes it to last_tick_summary.
        """
        if not self._tick_lock.acquire(blocking=False):
            return {
                "ok": False,
                "skipped": "another tick is already running",
                "trigger": trigger,
            }
        try:
            return self._run_one_tick_locked(trigger)
        finally:
            self._tick_lock.release()

    def _run_one_tick_locked(self, trigger: str) -> dict:
        # Per-tick budget composed with the rolling daily cap. Daily
        # budget reads/writes overseer_state so it survives restarts.
        daily = DailyBudget(
            db=self._db,
            max_cost_usd=float(self._cfg.get(
                "loop_daily_budget_usd", 1.00)),
            max_calls=int(self._cfg.get(
                "loop_daily_budget_calls", 25)),
        )
        budget = TickBudget(
            max_calls=int(self._cfg.get("loop_max_llm_calls_per_tick", 10)),
            max_cost_usd=float(self._cfg.get("loop_max_cost_usd_per_tick", 0.50)),
            daily_budget=daily,
        )
        summary: dict[str, Any] = {
            "ok": True,
            "trigger": trigger,
            "started_at": _utc_iso(),
            "sessions_summarized": 0,
            "sessions_failed": 0,
            "sessions_empty": 0,
            "notes_tagged": 0,
            "notes_failed": 0,
            "working_memory_rebuilt": False,
            "skipped_due_to_budget": [],
            "errors": [],
        }

        # ── Step 0: temporal cadence (Slice 5 CP2 — promoted from
        # last to first in Slice 5.5). Daily/weekly/monthly are time-
        # anchored — they only have ONE chance to fire per period.
        # Running them first guarantees they get budget even when
        # earlier-in-day work has loaded the queue.
        # No-op outside the 22:00-local trigger windows; cheap to call.
        # Slice 5.6 bypass design: temporal cadence runs UNCONDITIONALLY
        # of the daily budget. The bypass is implemented inside
        # _run_temporal_cadence; gating Step 0 itself on budget would
        # negate the bypass. Time-anchored narratives only get one
        # chance per period — never starve them.
        if self._cfg.get("temporal_cadence_loop_enabled", True):
            try:
                self._run_temporal_cadence(
                    budget=budget, summary=summary)
            except Exception as e:
                self._log.exception("temporal cadence step failed: %s", e)
                summary["errors"].append(
                    "temporal_cadence: " + str(e)[:200])

        # Step 1: summarize completed sessions
        if self._cfg.get("loop_auto_summarize_sessions", True):
            try:
                self._summarize_completed_sessions(budget, summary)
            except Exception as e:
                self._log.exception("summarize step failed: %s", e)
                summary["errors"].append("summarize: " + str(e)[:200])

        # Step 1b: classification refresh + import processing
        # Always refresh classification (cheap, just COUNT/AVG queries)
        # before deciding what to do with each import.
        if self._cfg.get("loop_auto_classify", True):
            try:
                changes = self._db.auto_classify_projects()
                summary["classify_changed"] = sum(
                    1 for c in changes if c.get("changed_to"))
            except Exception as e:
                self._log.exception("auto-classify failed: %s", e)
                summary["errors"].append("classify: " + str(e)[:200])

        # Step 1c: summarize imports (per-classification routing).
        if (self._cfg.get("loop_summarize_imports", True)
                and not budget.exhausted()):
            try:
                self._summarize_imported_sessions(budget, summary)
            except Exception as e:
                self._log.exception("summarize-imports step failed: %s", e)
                summary["errors"].append("summarize_imports: " + str(e)[:200])

        # Step 1c.5 (Slice 9.4 CP2): periodic GitHub ingest.
        # Runs on its own schedule via interval_hours, not every tick.
        # On a successful run the freshly-ingested imported_sessions
        # rows are picked up by the NEXT tick's _summarize_imported_
        # sessions step — we deliberately don't loop them through in
        # the same tick to keep budget accounting clean.
        if self._cfg.get("loop_git_ingest_enabled", False):
            try:
                import git_ingest as _gi
                summary["git_ingest"] = _gi.run_scheduled(
                    self._db, self._cfg, self._log)
            except Exception as e:
                self._log.exception("git_ingest step failed: %s", e)
                summary["errors"].append("git_ingest: " + str(e)[:200])

        # Step 1d: generate missing automation rollups (cheap, Sonnet 4.6).
        if (self._cfg.get("loop_run_rollups", True)
                and not budget.exhausted()):
            try:
                self._generate_missing_rollups(budget, summary)
            except Exception as e:
                self._log.exception("rollups step failed: %s", e)
                summary["errors"].append("rollups: " + str(e)[:200])

        # Step 2: auto-tag untagged notes
        if (self._cfg.get("loop_auto_tag_notes", True)
                and not budget.exhausted()):
            try:
                self._tag_untagged_notes(budget, summary)
            except Exception as e:
                self._log.exception("tag step failed: %s", e)
                summary["errors"].append("tag: " + str(e)[:200])

        # Step 3: working memory always rebuilds (no LLM call)
        if self._cfg.get("loop_build_working_memory", True):
            try:
                wm = self.build_working_memory()
                self._db.set_overseer_state(
                    "working_memory_json", json.dumps(wm))
                self._db.set_overseer_state(
                    "working_memory_built_at", _utc_iso())
                summary["working_memory_rebuilt"] = True
                summary["working_memory_keys"] = list(wm.keys())
            except Exception as e:
                self._log.exception("working memory build failed: %s", e)
                summary["errors"].append("working_memory: " + str(e)[:200])

        # Step 4: notification rules (deterministic, no LLM cost).
        if self._cfg.get("loop_run_notifications", True):
            try:
                notif_summary = evaluate_rules(
                    db=self._db, core_memory=self._core,
                    config=self._cfg)
                summary["notifications"] = notif_summary
            except Exception as e:
                self._log.exception("notifications step failed: %s", e)
                summary["errors"].append("notifications: " + str(e)[:200])

        # Step 5: overseer journal — write the instance's reflection
        # on what just happened. Skipped automatically when nothing
        # notable occurred (see is_tick_notable). Uses Sonnet by
        # default; ~$0.005/entry. The journal is the thinking layer
        # — future instances boot-read it before the structured
        # tables. Per locked design (3f.5): "you need both guidance
        # AND thinking; future_overseer_notes is guidance, journal
        # is thinking."
        #
        # Slice 5.5 cadence-calibration gates: the journal previously
        # wrote on every notable tick (215 entries in 3 days, eating
        # 70-80% of the daily budget before the temporal step could
        # fire). Two gates now apply BEFORE the LLM call:
        #   1. cooldown — at least N minutes since the last entry
        #   2. daily cap — at most M entries per LOCAL day
        # Both are configurable via plugin.toml.
        if (self._cfg.get("loop_journal_enabled", True)
                and self._journal_cadence_ok(summary)):
            try:
                # Use the just-built working memory (cached in state)
                # so the journal sees the same view a chat would.
                wm_json = self._db.get_overseer_state(
                    "working_memory_json")
                wm = None
                if wm_json:
                    try:
                        wm = json.loads(wm_json)
                    except Exception:
                        wm = None
                # Slice 9.9 (2026-05-20): journal step is now tool-
                # enabled. Pass core_memory + sibling_daily_cap so the
                # tool dispatcher has everything it needs. Sibling
                # dispatch is blocked at the journal layer (defense
                # in depth) but the cap is still threaded in case a
                # future allowed-tool needs it.
                _sib_cap = int(self._cfg.get(
                    "loop_daily_sibling_dispatches", 20))
                # 9.9: inject pending_notification_responses into the
                # tick summary so is_tick_notable() fires the journal
                # step when Tory has clicked something. Without this,
                # quiet ticks (no imports, no rollups) wouldn't trigger
                # the journal even when there's a queue of his clicks
                # waiting to be acted on.
                _pending_resp = (wm or {}).get(
                    "pending_notification_responses", 0)
                if _pending_resp:
                    summary["pending_notification_responses"] = _pending_resp
                jid = write_tick_journal_entry(
                    db=self._db, llm=self._llm,
                    tick_summary=summary,
                    working_memory=wm,
                    budget=budget,
                    instance_id="overseer@" + (
                        self._stats.get("started_at") or "unknown"),
                    core_memory=self._core,
                    sibling_daily_cap=_sib_cap,
                )
                if jid:
                    summary["journal_entry_id"] = jid
            except Exception as e:
                self._log.exception("journal step failed: %s", e)
                summary["errors"].append("journal: " + str(e)[:200])

        # ── Step 6: insight scans (3h CP2) ───────────────────
        # Conservative auto-loop: Sonnet reads gist arcs for projects
        # that haven't been scanned in N hours, proposes new theme/
        # pattern/drift candidates. Capped per-project AND per-tick.
        # Default policy is "active+human" — automation projects
        # require explicit opt-in. Manual /insight/scan-now ignores
        # all of this and works on any tag.
        if (self._cfg.get("insight_loop_enabled", True)
                and not budget.exhausted()):
            try:
                self._run_insight_scans(budget=budget, summary=summary)
            except Exception as e:
                self._log.exception("insight scan step failed: %s", e)
                summary["errors"].append("insight: " + str(e)[:200])

        # ── Step 7: distill corrections (3i CP2) ─────────────
        # Periodic Sonnet pass that clusters uncondidated user
        # corrections into proposed blindspots. Quiet by default —
        # fires only when there's enough material AND enough time has
        # passed since the last distill. Cost ~$0.005-0.02 per run.
        if (self._cfg.get("distill_loop_enabled", True)
                and not budget.exhausted()):
            try:
                self._run_distill_corrections(
                    budget=budget, summary=summary)
            except Exception as e:
                self._log.exception("distill step failed: %s", e)
                summary["errors"].append("distill: " + str(e)[:200])

        # ── Step 8: project narrative refresh (Slice 4 CP1b) ─
        # Per-project Sonnet rollup of stats + recent gists into a
        # 3-paragraph narrative. Two gates per project (24h elapsed
        # AND ≥3 new sessions since last regen) and a hard cap of N
        # projects per tick so a fresh backfill of 47 projects can't
        # blow the daily LLM budget in a single tick.
        if (self._cfg.get("project_narrative_loop_enabled", True)
                and not budget.exhausted()):
            try:
                self._run_project_narrative_refresh(
                    budget=budget, summary=summary)
            except Exception as e:
                self._log.exception("project narrative step failed: %s", e)
                summary["errors"].append(
                    "project_narrative: " + str(e)[:200])

        # (Step 9 — temporal cadence — was here. Moved to Step 0 in
        # Slice 5.5 so time-anchored daily/weekly/monthly narratives
        # run before any other LLM step claims the day's budget.)

        # ── Step 10: Category B agent GC (Slice 10, 2026-05-20) ──
        # Cheap maintenance — drops b_invocation_transcripts past their
        # retained_until horizon (default 30 days). Once-per-day gated
        # so it doesn't run on every tick. Wrapped in try/except because
        # an old install without the table should never block the loop.
        try:
            last_gc = self._db.get_overseer_state("b_agent_gc_last_at")
            now_iso = _utc_iso()
            do_gc = True
            if last_gc:
                # crude day comparison: same YYYY-MM-DD → skip
                if last_gc[:10] == now_iso[:10]:
                    do_gc = False
            if do_gc:
                deleted = self._db.b_agent_gc_expired()
                self._db.set_overseer_state("b_agent_gc_last_at", now_iso)
                if deleted:
                    summary["b_agent_gc_deleted"] = deleted
        except AttributeError:
            # Old install without b_agent_gc_expired — log once
            self._log.debug("b_agent_gc_expired not available (old install)")
        except Exception as e:
            self._log.exception("b_agent_gc step failed: %s", e)
            summary["errors"].append("b_agent_gc: " + str(e)[:200])

        # ── Step 11: C graduation check (Slice 10 CP5) ───────────
        # Once-per-day scan for B patterns that meet the graduation
        # bar (≥10 dispatches + ≥7 rated 4+ in 7-day window). When
        # found AND no C row exists for that B yet, emit a notification
        # with custom actions [Promote / Keep as B / Explain]. The
        # accept handler creates the C row via accept_c_promotion()
        # called from chat_tools when overseer processes Tory's reply.
        # Per locked design (agent_ecosystem_design.md): graduation is
        # NEVER automatic — Tory accepts or rejects each proposal.
        # NOTE: removed once-per-day gate (2026-05-20 directive from
        # Tory). Original concern was notification spam, but the
        # rule_key='c_grad:<b_name>' dedup at the notifications layer
        # handles that — re-firing the check every tick can't produce
        # duplicate notifications. Cost is one cheap stats query per
        # tick. Running every tick keeps the shake-out feedback loop
        # short: as soon as overseer dispatches enough Bs, the
        # proposal appears.
        if self._cfg.get("c_graduation_check_enabled", True):
            try:
                # Slice 10 CP5: thresholds are configurable for
                # shake-out testing. Defaults (in overseer_db.py
                # class constants) match the locked design (10/7/7);
                # plugin.toml ships lower values (2/1/1) for early
                # production until one happy-path graduation
                # round-trip confirms the flow.
                proposals = self._db.check_c_graduations(
                    min_dispatches=int(self._cfg.get(
                        "c_graduation_min_dispatches",
                        self._db.C_GRADUATION_MIN_DISPATCHES)),
                    min_rated_4plus=int(self._cfg.get(
                        "c_graduation_min_rated_4plus",
                        self._db.C_GRADUATION_MIN_RATED_4PLUS)),
                    window_days=int(self._cfg.get(
                        "c_graduation_window_days",
                        self._db.C_GRADUATION_WINDOW_DAYS)),
                )
                if proposals:
                    summary["c_graduation_proposals"] = len(proposals)
                    for p in proposals:
                        self._emit_c_graduation_proposal(p)
            except AttributeError:
                self._log.debug(
                    "check_c_graduations not available (old install)")
            except Exception as e:
                self._log.exception("c_graduation step failed: %s", e)
                summary["errors"].append(
                    "c_graduation: " + str(e)[:200])

        # ── Step 12: Scheduled C-agent runs (Slice 10 CP5) ────────
        # For each due C agent (cadence_minutes elapsed since last_run_at
        # or never-run), dispatch the underlying B with the C's frozen
        # snapshot args. The same b_agents.dispatch_b_agent() path is
        # reused — C is just "B with a schedule" until specialization
        # (e.g. fine-tuned model swap) happens. C invocations carry
        # target='c-agent:<name>' in sibling_tasks for audit separation.
        if self._cfg.get("c_agents_run_enabled", True):
            try:
                self._run_scheduled_c_agents(budget=budget, summary=summary)
            except AttributeError:
                self._log.debug("c_agents not available (old install)")
            except Exception as e:
                self._log.exception("c_agents run failed: %s", e)
                summary["errors"].append("c_agents: " + str(e)[:200])

        # Tally + persist
        summary["finished_at"] = _utc_iso()
        summary["budget"] = budget.remaining()

        self._stats["ticks_run"] += 1
        self._stats["last_tick_at"] = summary["finished_at"]
        self._stats["last_tick_summary"] = summary
        self._db.set_overseer_state("last_tick_at", summary["finished_at"])

        self._log.info(
            "tick %s done: sessions=%d notes=%d wm=%s calls=%d cost=$%s err=%d",
            trigger, summary["sessions_summarized"], summary["notes_tagged"],
            summary["working_memory_rebuilt"],
            summary["budget"]["calls_used"],
            summary["budget"]["cost_used_usd"],
            len(summary["errors"]),
        )
        return summary

    # ── Step 1: summarize completed sessions ─────────────────────

    SESSIONS_MARK_KEY = "sessions_high_water_at"

    def _summarize_completed_sessions(self, budget: TickBudget,
                                      summary: dict) -> None:
        """Process sessions ended AFTER the high-water mark. On first
        tick, set the mark to MAX(ended_at) and do no work — that's
        Tory's locked policy: scheduled ticks are forward-only; opt-in
        backfill goes through POST /plugins/overseer/backfill.
        """
        mark = self._db.get_overseer_state(self.SESSIONS_MARK_KEY)
        if mark is None:
            anchor = self._anchor_sessions_mark()
            summary["sessions_anchor_set"] = anchor
            self._log.info(
                "first tick — anchored sessions mark to %s; no session "
                "work this tick (use POST /backfill for historical)",
                anchor)
            return

        # Forward-only: only sessions ended STRICTLY after the mark.
        candidates = self._core.query(
            "SELECT id, ai_platform, hostname, started_at, ended_at, "
            "summary, projects FROM sessions "
            "WHERE ended_at IS NOT NULL AND ended_at > ? "
            "ORDER BY ended_at ASC LIMIT 50",
            (mark,),
        )
        new_mark = mark
        for s in candidates:
            if budget.exhausted():
                summary["skipped_due_to_budget"].append(
                    "session:{}".format(s["id"]))
                break
            if self._db.is_session_processed(s["id"]):
                # Edge case (shouldn't happen with the mark, but harmless)
                if s.get("ended_at"):
                    new_mark = max(new_mark, s["ended_at"])
                continue
            try:
                outcome = self._summarize_one_session(s, budget, summary)
                if outcome == "summarized":
                    summary["sessions_summarized"] += 1
                elif outcome == "empty":
                    summary["sessions_empty"] += 1
                else:
                    summary["sessions_failed"] += 1
            except Exception as e:
                self._log.exception("summarize session %s failed: %s",
                                    s["id"], e)
                summary["sessions_failed"] += 1
                self._db.mark_session_processed(
                    s["id"], error=str(e)[:500])
            if s.get("ended_at"):
                new_mark = max(new_mark, s["ended_at"])

        if new_mark != mark:
            self._db.set_overseer_state(self.SESSIONS_MARK_KEY, new_mark)

    def _anchor_sessions_mark(self) -> str:
        """Set sessions high-water-mark to current MAX(ended_at) — or NOW
        if cortex.db has no ended sessions yet. Returns the value set."""
        rows = self._core.query(
            "SELECT MAX(ended_at) AS max_ended FROM sessions "
            "WHERE ended_at IS NOT NULL"
        )
        anchor = (rows[0].get("max_ended") if rows else None) or _utc_iso()
        self._db.set_overseer_state(self.SESSIONS_MARK_KEY, anchor)
        return anchor

    # ── Step 1b: summarize imported sessions ────────────────────

    def _summarize_imported_sessions(self, budget: TickBudget,
                                     summary: dict) -> None:
        """Find unprocessed imported_sessions and summarize each.

        No high-water mark — imports are explicit user actions, so we
        process every unprocessed one (subject to budget). Big bulk
        imports drain across multiple ticks at the per-tick cap.
        """
        rows = self._db.list_imported_sessions(limit=200)
        unprocessed = [r for r in rows
                       if not self._db.is_imported_processed(r["id"])]
        if not unprocessed:
            return

        for imp in unprocessed:
            if budget.exhausted():
                summary["skipped_due_to_budget"].append(
                    "imported:" + imp["id"])
                break
            try:
                outcome = self._summarize_one_imported(imp, budget, summary)
                key = {
                    "summarized": "imports_summarized",
                    "empty": "imports_empty",
                    "failed": "imports_failed",
                    "deferred": "imports_deferred",
                    "ignored": "imports_ignored",
                }.get(outcome, "imports_failed")
                summary.setdefault(key, 0)
                summary[key] += 1
            except Exception as e:
                self._log.exception(
                    "summarize import %s failed: %s", imp["id"], e)
                summary.setdefault("imports_failed", 0)
                summary["imports_failed"] += 1
                self._db.mark_imported_processed(
                    imp["id"], error=str(e)[:500])

    def _summarize_one_imported(self, imp: dict,
                                budget: TickBudget,
                                summary: dict | None = None) -> str:
        """Returns 'summarized' | 'empty' | 'failed' | 'deferred' | 'ignored'.

        Slice 3e: respects per-project classification:
          - human / auto    → individual gist (existing behavior)
          - automation      → mark processed; rollup step covers it
          - ignore          → mark processed; never summarized
        """
        if summary is None:
            summary = {}
        project = imp.get("project") or ""
        setting = self._db.get_project_setting(project)
        treat_as = setting.get("treat_as", "auto")
        if treat_as == "automation":
            self._db.mark_imported_processed(
                imp["id"], notes_used=0,
                error="deferred to automation rollup")
            return "deferred"
        if treat_as == "ignore":
            self._db.mark_imported_processed(
                imp["id"], notes_used=0, error="ignored by setting")
            return "ignored"

        src_path = Path(imp.get("source_path") or "")
        if not src_path.is_file():
            self._db.mark_imported_processed(
                imp["id"], error="file missing: " + str(src_path))
            return "failed"

        # Parse the .jsonl into messages
        try:
            metadata, messages = parse_claude_code_jsonl(src_path)
        except Exception as e:
            self._db.mark_imported_processed(
                imp["id"], error="parse failed: " + str(e)[:300])
            return "failed"

        if not messages:
            self._db.mark_imported_processed(
                imp["id"], notes_used=0, error="no messages in file")
            return "empty"

        # Build a transcript that fits in the prompt
        max_chars = int(self._cfg.get("loop_import_transcript_chars", 30000))
        transcript, stats = build_transcript_for_summary(
            messages, max_chars=max_chars)

        # Slice 13 (2026-05-21): sensitivity-aware prompt selection.
        # confidential / restricted sessions get the sanitized gist
        # prompt so the persisted gist carries structural signal but
        # no reconstructable minutia. `restricted` shouldn't normally
        # reach here (retention_policy='no-import'), but if one does,
        # the strictest prompt is the safe default.
        sensitivity = (imp.get("sensitivity") or "public").lower()
        gist_args = dict(
            imp_id=imp.get("id") or "",
            project=imp.get("project") or "(unknown)",
            cwd=imp.get("cwd") or "(unknown)",
            branch=imp.get("git_branch") or "(none)",
            started=imp.get("started_at") or "?",
            ended=imp.get("ended_at") or "?",
            dur=imp.get("duration_minutes") or 0,
            n_total=stats["messages_total"],
            u=imp.get("user_message_count") or 0,
            a=imp.get("assistant_message_count") or 0,
            n_used=stats["messages_used"],
            n_omit=stats["messages_omitted"],
            strategy=stats["strategy"],
            transcript=transcript,
        )
        if sensitivity in ("confidential", "restricted"):
            prompt = import_gist_prompt_sanitized(**gist_args)
            self._log.info(
                "import %s: sensitivity=%s → sanitized gist prompt",
                imp.get("id"), sensitivity)
        else:
            # Slice 3f.5 reframed prompt: gist drops all but THE CHANGE
            prompt = import_gist_prompt(**gist_args)
        if self._cfg.get("loop_paired_generation", True):
            paired = paired_generate(
                llm=self._llm, prompt=prompt,
                max_tokens=200, temperature=0.4,
                purpose="summarize-session",
            )
            budget.charge(paired["opus"])
            budget.charge(paired["gemma"])
            primary_result = paired["opus"]
        else:
            primary_result = self._llm.complete(
                prompt, max_tokens=200, temperature=0.4,
                purpose="summarize-session",
            )
            budget.charge(primary_result)
            paired = None

        if not primary_result.get("ok"):
            self._db.mark_imported_processed(
                imp["id"], notes_used=stats["messages_used"],
                error=primary_result.get("error", "")[:500])
            return "failed"

        gist_text = (primary_result.get("text") or "").strip().strip('"').strip()
        if not gist_text:
            self._db.mark_imported_processed(
                imp["id"], notes_used=stats["messages_used"],
                error="empty LLM response")
            return "failed"

        project_tag = (("project:" + imp["project"])
                       if imp.get("project") else None)
        # Slice 9.2 (overseer ask #4a): tag the gist with the ACTUAL
        # source of the imported session, not a hardcoded "claude-code"
        # literal. Before this fix, every gist (chatgpt, grok-com,
        # grok-twitter, claude-code) shared a single source tag, which
        # collapsed platform-specific behavior into one undifferentiated
        # bucket and made it impossible to filter gists by source. The
        # overseer flagged this explicitly when asked what would help.
        source = imp.get("source") or "unknown"
        tags = ["auto", "import-summary", f"source:{source}"]
        if project_tag:
            tags.append(project_tag)

        gist_id = self._db.add_gist(
            gist_text,
            period_label=f"{source}:" + (imp.get("id") or "")[-12:],
            period_start=imp.get("started_at"),
            period_end=imp.get("ended_at"),
            confidence="med",
            tags=tags,
        )
        if paired and paired.get("ok"):
            try:
                write_dialectic_row(
                    db=self._db, paired=paired,
                    artifact_type="gist", artifact_id=gist_id,
                    purpose="summarize-session",
                    source_context="import " + (imp.get("id") or "")[-12:]
                                   + ", " + str(stats["messages_used"])
                                   + " msgs",
                )
            except Exception as e:
                self._log.warning(
                    "dialectic write failed for import %s: %s",
                    imp["id"], e)

        # Slice 3f.5 #2: route this new gist against open questions
        self._route_gist(gist_id, gist_text, summary, budget)

        self._db.mark_imported_processed(
            imp["id"], gist_id=gist_id,
            notes_used=stats["messages_used"])
        return "summarized"

    def _route_gist(self, gist_id, gist_text, summary, budget):
        """Slice 3f.5 #2: route a newly-created gist against open
        questions. Side effects: file_evidence rows + lifecycle
        transitions (handled inside file_evidence) + emit a
        question_reactivated notification when applicable.
        """
        if not self._cfg.get("loop_evidence_routing", True):
            return
        if budget.exhausted():
            summary.setdefault("routing_skipped", 0)
            summary["routing_skipped"] += 1
            return
        try:
            r = route_evidence_to_questions(
                db=self._db, llm=self._llm,
                gist_text=gist_text, gist_id=gist_id,
                budget=budget,
            )
        except Exception as e:
            self._log.warning(
                "routing gist %s failed: %s", gist_id, e)
            return
        filings = r.get("filings") or []
        reactivated = r.get("reactivated") or []
        summary.setdefault("routings_filed", 0)
        summary["routings_filed"] += sum(
            1 for f in filings if f.get("newly_filed"))
        if not filings:
            summary.setdefault("routings_unfiled", 0)
            summary["routings_unfiled"] += 1
        for q in reactivated:
            summary.setdefault("questions_reactivated", [])
            summary["questions_reactivated"].append(q)
            try:
                self._db.emit_notification(
                    severity="warn",
                    title="Question reactivated: {}".format(
                        q["question"][:80]),
                    body=("Dormant question got new evidence — "
                          "this is a signal you may be circling back "
                          "to something you'd set aside."),
                    rule_name="question_reactivated",
                    rule_key="q-{}-gist-{}".format(
                        q["question_id"], gist_id),
                    related_table="open_questions",
                    related_id=str(q["question_id"]),
                )
            except Exception as e:
                self._log.warning(
                    "reactivation notification failed: %s", e)

    def process_imports_targeted(self, *, cwd_likes, limit=100,
                                  max_cost_usd=4.0,
                                  max_calls=None) -> dict:
        """Slice work-org (2026-05-21): process ONLY imported_sessions
        whose cwd matches one of the LIKE patterns in ``cwd_likes``.

        Used by POST /imports/process-targeted to drain a specific
        cohort (e.g. the work-computer ClientA sessions) without
        touching the rest of the unprocessed backlog. Bypasses the
        daily budget (escape hatch, like /backfill) but enforces a
        hard ``max_cost_usd`` cap so the caller controls spend.

        cwd_likes: list of SQL LIKE patterns, e.g.
            ['%ClientA%', '%workuser%', '%/home/workuser%']
        Returns a summary dict.
        """
        if not self._tick_lock.acquire(blocking=False):
            return {"ok": False,
                    "skipped": "a tick or backfill is already running"}
        try:
            if max_calls is None:
                max_calls = int(limit) + 10
            budget = TickBudget(
                max_calls=int(max_calls),
                max_cost_usd=float(max_cost_usd),
            )  # daily_budget=None → escape hatch, same as /backfill
            summary = {
                "ok": True, "kind": "imports-targeted",
                "started_at": _utc_iso(),
                "cwd_likes": list(cwd_likes),
                "skipped_due_to_budget": [],
                "errors": [],
            }
            # Find matching unprocessed sessions.
            where = " OR ".join(["cwd LIKE ?"] * len(cwd_likes))
            rows = self._db._conn.execute(
                f"SELECT * FROM imported_sessions "
                f"WHERE ({where}) "
                f"ORDER BY started_at ASC LIMIT ?",
                (*cwd_likes, int(limit) * 3),
            ).fetchall()
            candidates = [dict(r) for r in rows]
            unprocessed = [
                imp for imp in candidates
                if not self._db.is_imported_processed(imp["id"])
            ][:int(limit)]
            summary["matched_total"] = len(candidates)
            summary["unprocessed_targeted"] = len(unprocessed)

            for imp in unprocessed:
                if budget.exhausted():
                    summary["skipped_due_to_budget"].append(
                        "imported:" + imp["id"])
                    continue
                try:
                    outcome = self._summarize_one_imported(
                        imp, budget, summary)
                    key = {
                        "summarized": "imports_summarized",
                        "empty": "imports_empty",
                        "failed": "imports_failed",
                        "deferred": "imports_deferred",
                        "ignored": "imports_ignored",
                    }.get(outcome, "imports_failed")
                    summary.setdefault(key, 0)
                    summary[key] += 1
                except Exception as e:
                    self._log.exception(
                        "targeted import %s failed: %s", imp["id"], e)
                    summary.setdefault("imports_failed", 0)
                    summary["imports_failed"] += 1
                    self._db.mark_imported_processed(
                        imp["id"], error=str(e)[:500])

            # Rebuild working memory so the new gists land in context.
            try:
                wm = self.build_working_memory()
                self._db.set_overseer_state(
                    "working_memory_json", json.dumps(wm))
                self._db.set_overseer_state(
                    "working_memory_built_at", _utc_iso())
                summary["working_memory_rebuilt"] = True
            except Exception as e:
                summary["errors"].append("wm_rebuild: " + str(e)[:200])

            summary["finished_at"] = _utc_iso()
            summary["budget"] = budget.remaining()
            return summary
        finally:
            self._tick_lock.release()

    def _summarize_one_session(self, session: dict, budget: TickBudget,
                               summary: dict | None = None) -> str:
        """Returns 'summarized' | 'empty' | 'failed'."""
        # summary is the per-tick dict; used for routing counters.
        # Defaulting to a throwaway dict keeps backfill callers working.
        if summary is None:
            summary = {}
        notes = self._core.query(
            "SELECT created_at, content, note_type FROM notes "
            "WHERE session_id = ? ORDER BY created_at",
            (session["id"],),
        )
        if not notes:
            self._db.mark_session_processed(
                session["id"], notes_count=0)
            return "empty"

        max_notes = int(self._cfg.get(
            "loop_session_summary_max_notes", 50))
        body_lines = []
        for n in notes[:max_notes]:
            ts = (n.get("created_at") or "")[:16]
            content = (n.get("content") or "").strip()[:300]
            ntype = n.get("note_type") or "note"
            body_lines.append("- [{}] [{}] {}".format(ts, ntype, content))
        body = "\n".join(body_lines)
        # Slice 3f.5 reframed prompt: gist drops everything but THE CHANGE
        prompt = session_gist_prompt(
            session_id=session["id"],
            started_at=session.get("started_at"),
            ended_at=session.get("ended_at"),
            platform=session.get("ai_platform") or "unknown",
            notes_total=len(notes),
            notes_shown_msg=("" if len(notes) <= max_notes
                             else ", first {} shown".format(max_notes)),
            body=body,
        )
        # Paired generation if enabled — Opus and Gemma in parallel.
        # The diff between their outputs becomes a dialectic_open row.
        if self._cfg.get("loop_paired_generation", True):
            paired = paired_generate(
                llm=self._llm, prompt=prompt,
                max_tokens=160, temperature=0.4,
                purpose="summarize-session",
            )
            budget.charge(paired["opus"])
            budget.charge(paired["gemma"])
            primary_result = paired["opus"]
        else:
            primary_result = self._llm.complete(
                prompt, max_tokens=160, temperature=0.4,
                purpose="summarize-session",
            )
            budget.charge(primary_result)
            paired = None

        if not primary_result.get("ok"):
            self._db.mark_session_processed(
                session["id"], notes_count=len(notes),
                error=primary_result.get("error", "")[:500])
            return "failed"

        gist_text = (primary_result.get("text") or "").strip().strip('"').strip()
        if not gist_text:
            self._db.mark_session_processed(
                session["id"], notes_count=len(notes),
                error="empty LLM response")
            return "failed"

        platform = session.get("ai_platform") or ""
        gist_id = self._db.add_gist(
            gist_text,
            period_label="session:" + session["id"][:8],
            period_start=session.get("started_at"),
            period_end=session.get("ended_at"),
            confidence="med",
            tags=["auto", "session-summary"]
                 + (["platform:" + platform] if platform else []),
        )
        # Persist the dialectic if paired ran successfully on both sides.
        if paired and paired.get("ok"):
            try:
                write_dialectic_row(
                    db=self._db, paired=paired,
                    artifact_type="gist", artifact_id=gist_id,
                    purpose="summarize-session",
                    source_context=("session " + session["id"][:8]
                                    + ", " + str(len(notes)) + " notes"),
                )
            except Exception as e:
                self._log.warning(
                    "dialectic write failed for session %s: %s",
                    session["id"], e)

        # Slice 3f.5 #2: route this new gist against open questions
        self._route_gist(gist_id, gist_text, summary, budget)

        self._db.mark_session_processed(
            session["id"], gist_id=gist_id, notes_count=len(notes))
        return "summarized"

    # ── Step 2: auto-tag untagged notes ──────────────────────────

    NOTES_MARK_KEY = "notes_high_water_at"

    def _tag_untagged_notes(self, budget: TickBudget, summary: dict) -> None:
        """Forward-only tagging: notes created AFTER the high-water mark.
        First tick anchors the mark and does no work. Backfill bypasses
        the mark via /backfill."""
        mark = self._db.get_overseer_state(self.NOTES_MARK_KEY)
        if mark is None:
            anchor = self._anchor_notes_mark()
            summary["notes_anchor_set"] = anchor
            self._log.info(
                "first tick — anchored notes mark to %s; no tag work this "
                "tick (use POST /backfill for historical)", anchor)
            return

        candidates = self._core.query(
            "SELECT id, content, tags, project, note_type, source, "
            "session_id, created_at FROM notes "
            "WHERE created_at > ? AND (tags IS NULL OR tags = '') "
            "ORDER BY created_at ASC LIMIT 200",
            (mark,),
        )
        unprocessed = [n for n in candidates
                       if not self._db.is_note_processed(n["id"])]
        if not unprocessed:
            # Still update the mark to prevent rescan of the same window
            # next tick (in case all candidates were already processed).
            return

        batch_size = int(self._cfg.get("loop_tag_batch_size", 10))
        max_per_note = int(self._cfg.get("loop_tag_max_per_note", 3))
        new_mark = mark
        for i in range(0, len(unprocessed), batch_size):
            if budget.exhausted():
                summary["skipped_due_to_budget"].append(
                    "notes-batch@{}".format(i))
                break
            batch = unprocessed[i:i + batch_size]
            try:
                tagged_n, failed_n = self._tag_one_batch(
                    batch, budget, max_per_note)
                summary["notes_tagged"] += tagged_n
                summary["notes_failed"] += failed_n
            except Exception as e:
                self._log.exception(
                    "tag batch starting at %d failed: %s", i, e)
                summary["notes_failed"] += len(batch)
            for n in batch:
                if n.get("created_at"):
                    new_mark = max(new_mark, n["created_at"])

        if new_mark != mark:
            self._db.set_overseer_state(self.NOTES_MARK_KEY, new_mark)

    def _anchor_notes_mark(self) -> str:
        rows = self._core.query("SELECT MAX(created_at) AS max_c FROM notes")
        anchor = (rows[0].get("max_c") if rows else None) or _utc_iso()
        self._db.set_overseer_state(self.NOTES_MARK_KEY, anchor)
        return anchor

    def _tag_one_batch(self, batch: list[dict], budget: TickBudget,
                       max_per_note: int) -> tuple[int, int]:
        # Build numbered prompt
        lines = []
        for idx, note in enumerate(batch, 1):
            content = (note.get("content") or "").strip()[:280]
            project = (note.get("project") or "").strip()
            ntype = note.get("note_type") or "note"
            extra = " (project={})".format(project) if project else ""
            lines.append("{}. [{}] {}{}".format(idx, ntype, content, extra))
        body = "\n".join(lines)
        prompt = (
            "For each note below, give 1-{n} short namespaced tags.\n"
            "Tag format: 'namespace:slug'. Examples: topic:llm, "
            "project:cortex, theme:hardware, method:dialectic, "
            "tool:openrouter, person:tory.\n"
            "Use established namespaces when they fit; invent new "
            "namespaces sparingly. Keep slugs short and lowercase.\n\n"
            "Notes:\n{body}\n\n"
            "Reply with EXACTLY one line per note, in this format:\n"
            "1. tag1, tag2, tag3\n"
            "2. tag1\n"
            "...\n"
            "If a note is too vague to tag, write `(none)`.\n"
            "No preamble. No explanations. No code fences. "
            "Just the numbered tag lines."
        ).format(n=max_per_note, body=body)

        result = self._llm.complete(
            prompt, max_tokens=400, temperature=0.2,
            purpose="auto-tag-notes",
        )
        budget.charge(result)

        if not result.get("ok"):
            err = result.get("error", "")[:500]
            for note in batch:
                self._db.mark_note_processed(note["id"], error=err)
            return 0, len(batch)

        tag_lists = parse_tag_lines(
            result.get("text") or "", len(batch),
            max_per_note=max_per_note)

        tagged_n = 0
        for note, tags in zip(batch, tag_lists):
            try:
                if tags:
                    self._db.tag_many("notes", note["id"], tags)
                    self._db.mark_note_processed(
                        note["id"], tags_added=",".join(tags))
                    tagged_n += 1
                else:
                    # No usable tags, but the LLM did respond — mark
                    # processed so we don't re-ask on every tick.
                    self._db.mark_note_processed(note["id"])
            except Exception as e:
                self._log.warning(
                    "tag write failed for note %s: %s", note["id"], e)
                self._db.mark_note_processed(
                    note["id"], error=str(e)[:200])
        return tagged_n, 0

    # ── Step 1d: automation rollups (Sonnet 4.6) ────────────────

    def _generate_missing_rollups(self, budget: TickBudget,
                                  summary: dict) -> None:
        """For each project classified 'automation', find dates that
        have imports but no rollup yet, and generate up to N rollups
        per tick. Cheap (Sonnet) — but still respects the budget.

        Strategy: oldest-first so backlog drains chronologically.
        """
        max_per_tick = int(self._cfg.get(
            "loop_rollups_max_per_tick", 5))
        n_made = 0

        # Find automation projects
        rows = self._db._conn.execute(
            "SELECT project FROM imported_project_settings "
            "WHERE treat_as = 'automation'"
        ).fetchall()
        automation_projects = [r[0] for r in rows]
        if not automation_projects:
            return

        for project in automation_projects:
            if n_made >= max_per_tick or budget.exhausted():
                break
            dates = self._db.imports_dates_for_project(project)
            for d in dates:
                if n_made >= max_per_tick or budget.exhausted():
                    break
                if self._db.get_rollup(project, d):
                    continue   # already rolled up
                try:
                    res = generate_rollup(
                        db=self._db, llm=self._llm,
                        project=project, rollup_date=d, budget=budget,
                    )
                    if res.get("ok"):
                        n_made += 1
                        summary.setdefault("rollups_generated", 0)
                        summary["rollups_generated"] += 1
                        if res.get("anomaly"):
                            summary.setdefault(
                                "rollups_anomalies", 0)
                            summary["rollups_anomalies"] += 1
                    else:
                        summary.setdefault("rollups_failed", 0)
                        summary["rollups_failed"] += 1
                except Exception as e:
                    self._log.exception(
                        "rollup %s/%s failed: %s", project, d, e)
                    summary.setdefault("rollups_failed", 0)
                    summary["rollups_failed"] += 1

    # ── Step 6: insight scans (3h CP2) ──────────────────────────

    def _run_insight_scans(self, *, budget: TickBudget, summary: dict):
        """Pick a few projects due for an insight scan and run them.

        Conservative on purpose: caps per-tick (max_projects), per-
        project cadence (scan_interval_hours), and project policy
        (active+human only by default). The daily budget caps the
        whole thing on top.
        """
        policy = str(self._cfg.get(
            "insight_loop_project_policy", "active+human"))
        if policy == "never":
            return
        interval = int(self._cfg.get(
            "insight_loop_scan_interval_hours", 24))
        max_per_tick = int(self._cfg.get(
            "insight_loop_max_projects_per_tick", 2))
        max_cost = float(self._cfg.get(
            "insight_scan_max_cost_usd_per_scan", 0.05))
        days = int(self._cfg.get(
            "insight_scan_default_days", 7))

        projects = select_eligible_projects(
            db=self._db, policy=policy,
            scan_interval_hours=interval, max_projects=max_per_tick,
        )

        summary["insight_projects_eligible"] = len(projects)
        if not projects:
            return

        total_proposed = 0
        total_deduped = 0
        scans_done = 0
        for project in projects:
            if budget.exhausted():
                summary["errors"].append(
                    "insight: budget exhausted after %d scan(s)" % scans_done)
                break
            try:
                r = scan_project_arcs(
                    db=self._db, llm=self._llm, project=project,
                    days=days, max_cost_usd=max_cost,
                    budget=budget,
                    triggered_by="loop",
                )
            except Exception as e:
                self._log.exception(
                    "insight scan for %s failed: %s", project, e)
                summary["errors"].append(
                    "insight:%s: %s" % (project, str(e)[:140]))
                continue
            scans_done += 1
            if r.get("ok"):
                total_proposed += int(r.get("candidates_proposed") or 0)
                total_deduped += int(r.get("candidates_deduped") or 0)
            else:
                summary["errors"].append(
                    "insight:%s: %s" % (project, r.get("error", "")[:140]))

        summary["insight_scans_run"] = scans_done
        summary["insight_candidates_proposed"] = total_proposed
        summary["insight_candidates_deduped"] = total_deduped

    # ── Step 7: distill corrections (3i CP2) ────────────────────

    def _journal_cadence_ok(self, summary: dict) -> bool:
        """Slice 5.5 cadence-calibration gate. Returns True when the
        loop is allowed to write a journal entry this tick.

        Two gates compose:
          - cooldown: at least N minutes since the last entry
            (loop_journal_min_minutes_between, default 90)
          - daily cap: at most M entries per LOCAL day
            (loop_journal_max_per_local_day, default 6)

        Records the skip reason in summary["journal_skipped"] so the
        tick log surfaces it without an exception.
        """
        max_per_day = int(self._cfg.get(
            "loop_journal_max_per_local_day", 6))
        min_minutes = int(self._cfg.get(
            "loop_journal_min_minutes_between", 90))

        # Cooldown gate
        last_at = self._db.last_journal_written_at()
        if last_at:
            try:
                last_dt = datetime.fromisoformat(
                    last_at.replace(" ", "T"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                gap_min = (datetime.now(timezone.utc) - last_dt
                           ).total_seconds() / 60.0
                if gap_min < min_minutes:
                    summary["journal_skipped"] = (
                        "cooldown ({:.0f}m < {}m)".format(
                            gap_min, min_minutes))
                    return False
            except Exception:
                pass  # if we can't parse, don't block

        # Local-day cap gate. Bound = local-midnight expressed in UTC,
        # using the temporal helper so it stays consistent with the
        # narrative period bounds. max_per_day <= 0 means unlimited
        # (Slice 8: cooldown is the only floor on cadence).
        if max_per_day > 0:
            try:
                local_start_utc, _, _ = T_clock.today_local_bounds()
                n_today = self._db.journal_count_since(local_start_utc)
                if n_today >= max_per_day:
                    summary["journal_skipped"] = (
                        "daily cap ({}/{})".format(n_today, max_per_day))
                    return False
            except Exception:
                pass  # if local bounds fail, don't block

        return True

    def _run_distill_corrections(self, *, budget: TickBudget,
                                  summary: dict):
        """Periodic distill pass over uncondidated corrections.

        Two gates before the LLM call: (1) enough new material
        accumulated, (2) enough time since the last distill. Both are
        configurable. Cheap when it does fire — single Sonnet call.
        """
        min_corrections = int(self._cfg.get(
            "distill_loop_min_corrections", 3))
        interval_hours = int(self._cfg.get(
            "distill_loop_interval_hours", 24))
        max_cost = float(self._cfg.get(
            "insight_scan_max_cost_usd_per_scan", 0.05))

        n_undistilled = self._db.correction_count(undistilled_only=True)
        if n_undistilled < min_corrections:
            return  # nothing to do, no log entry needed

        # When did we last distill? Look in insight_scans for kind=
        # 'corrections-distill'.
        recent = self._db.recent_insight_scans(limit=20)
        last = None
        for s in recent:
            if s.get("scan_kind") == "corrections-distill":
                last = s.get("scanned_at")
                break
        if last:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(hours=interval_hours)).strftime(
                          "%Y-%m-%d %H:%M:%S")
            if last >= cutoff:
                return  # too soon

        try:
            r = distill_uncondidated_corrections(
                db=self._db, llm=self._llm,
                max_cost_usd=max_cost,
                budget=budget,
                triggered_by="loop",
            )
        except Exception as e:
            self._log.exception("distill failed: %s", e)
            summary["errors"].append("distill: " + str(e)[:200])
            return

        if r.get("ok"):
            summary["distill_corrections_seen"] = r.get(
                "corrections_seen", 0)
            summary["distill_blindspots_proposed"] = r.get(
                "candidates_proposed", 0)
            summary["distill_cost_usd"] = r.get("cost_usd", 0.0)
        else:
            summary["errors"].append(
                "distill: " + (r.get("error") or "")[:140])

    # ── Step 8: project narrative refresh (Slice 4 CP1b) ────────

    def _run_project_narrative_refresh(self, *, budget: TickBudget,
                                       summary: dict):
        """Sonnet narrative regen for projects whose data has moved
        enough since their last narrative.

        Decision tree per project:
          1. Refresh deterministic stats (cheap; we want the latest
             session_count etc. for the regen-trigger comparison).
          2. needs_regen() decides based on time + session-count
             thresholds (config-overridable; defaults 24h and 3
             sessions).
          3. If yes, generate narrative; charge cost to budget.

        Bails as soon as either the per-tick max projects cap or the
        daily/per-tick cost budget is hit. Loops fire every few minutes
        so the next tick will continue where this one left off.
        """
        max_per_tick = int(self._cfg.get(
            "project_narrative_max_per_tick", 3))
        max_cost = float(self._cfg.get(
            "project_narrative_max_cost_usd_per_call",
            project_narrative.DEFAULT_MAX_COST_USD_PER_CALL,
        ))
        min_hours = int(self._cfg.get(
            "project_narrative_min_hours_between_regen",
            project_narrative.DEFAULT_MIN_HOURS_BETWEEN_REGEN,
        ))
        min_new_sessions = int(self._cfg.get(
            "project_narrative_min_new_sessions",
            project_narrative.DEFAULT_MIN_NEW_SESSIONS,
        ))

        try:
            projects = self._db.list_distinct_imported_projects()
        except Exception as e:
            self._log.warning("listing projects failed: %s", e)
            return

        regenerated = 0
        regen_cost = 0.0
        regen_failures = 0
        # Single pass; respect per-tick cap.
        for project in projects:
            if regenerated >= max_per_tick or budget.exhausted():
                break

            # Refresh deterministic stats first so the regen check
            # sees the current session_count.
            try:
                project_summary.refresh_summary(self._db, project)
            except Exception as e:
                self._log.warning(
                    "stats refresh failed for %s: %s", project, e)
                continue

            row = self._db.get_project_summary(project)
            should, reason = project_narrative.needs_regen(
                summary_row=row,
                min_hours_between=min_hours,
                min_new_sessions=min_new_sessions,
            )
            if not should:
                continue

            self._log.info(
                "regen narrative for %s — %s", project, reason)

            # Reconstruct the parsed view that generate_narrative wants.
            # The DB row has top_files/models as JSON strings; parse
            # them so the prompt formatter sees the lists/dicts.
            stats_for_prompt = dict(row)
            try:
                stats_for_prompt["top_files"] = json.loads(
                    row.get("top_files_json") or "[]")
            except Exception:
                stats_for_prompt["top_files"] = []
            try:
                stats_for_prompt["models_used"] = json.loads(
                    row.get("models_used_json") or "{}")
            except Exception:
                stats_for_prompt["models_used"] = {}

            try:
                gen = project_narrative.generate_narrative(
                    db=self._db, llm=self._llm,
                    project=project, stats=stats_for_prompt,
                    max_cost_usd=max_cost,
                    triggered_by="loop",
                )
            except Exception as e:
                self._log.exception(
                    "narrative generation crashed for %s: %s",
                    project, e)
                regen_failures += 1
                continue

            cost = float(gen.get("cost_usd") or 0.0)
            # Budget charging — same shape as other steps via TickBudget
            # synthetic charge (no LLM result wrapper to pass).
            try:
                budget.charge({
                    "ok": True,
                    "cost_usd": cost,
                    "prompt_tokens": gen.get("prompt_tokens", 0),
                    "completion_tokens": gen.get("completion_tokens", 0),
                })
            except Exception:
                pass

            if not gen.get("ok"):
                regen_failures += 1
                self._log.warning(
                    "narrative gen failed for %s: %s",
                    project, gen.get("error", "?"))
                continue

            try:
                project_narrative.apply_narrative(
                    db=self._db, project=project,
                    narrative_text=gen["narrative"],
                    cost_usd=cost,
                    session_count_at_update=row.get(
                        "session_count", 0),
                )
            except Exception as e:
                self._log.exception(
                    "apply_narrative failed for %s: %s", project, e)
                regen_failures += 1
                continue

            regenerated += 1
            regen_cost += cost

        if regenerated > 0 or regen_failures > 0:
            summary["project_narratives_regenerated"] = regenerated
            summary["project_narratives_failed"] = regen_failures
            summary["project_narratives_cost_usd"] = round(regen_cost, 4)

    # ── Step 9: temporal cadence (Slice 5 CP2) ──────────────────

    # ── Slice 10 CP5 (2026-05-20): C-graduation + scheduled runs ────

    def _emit_c_graduation_proposal(self, proposal: dict) -> None:
        """Emit a notification with custom actions when a B pattern
        meets the graduation bar. Tory accepts, rejects, or asks for
        explanation via the action buttons.

        Idempotent against repeated proposals: notification rule_key
        is 'c_grad:<b_name>' so re-emits dedupe at the notification
        layer (notifications schema already has dedup-by-rule_key).
        """
        b_name = proposal["b_agent_name"]
        c_name = proposal["proposed_c_name"]
        d = proposal["dispatches"]
        r4 = proposal["rated_4_plus"]
        title = f"Promote B agent '{b_name}' to C?"
        body = (
            f"`{b_name}` has crossed the graduation bar: {d} dispatches "
            f"in the past 7 days, {r4} of them rated 4+ by overseer. "
            f"Proposed C name: `{c_name}` (24h cadence by default). "
            f"C will reuse the B's system prompt frozen at promotion "
            f"time and run on a schedule. You can promote, keep as B, "
            f"or ask overseer to explain the difference."
        )
        actions = [
            {
                "label": "Promote to C",
                "kind": "promote_b_to_c",
                "payload": {
                    "b_agent_name": b_name,
                    "proposed_c_name": c_name,
                    "dispatches": d,
                    "rated_4_plus": r4,
                },
            },
            {
                "label": "Keep as B",
                "kind": "keep_as_b",
                "payload": {"b_agent_name": b_name},
            },
            {
                "label": "Explain",
                "kind": "explain_c_graduation",
                "payload": {"b_agent_name": b_name},
            },
        ]
        try:
            self._db.emit_notification(
                severity="info",
                title=title,
                body=body,
                rule_name="c-graduation-proposal",
                rule_key=f"c_grad:{b_name}",
                related_table="c_agents",
                related_id=b_name,
                actions=actions,
            )
            self._log.info(
                "c-graduation proposal emitted: %s (d=%d, r4+=%d)",
                b_name, d, r4)
        except Exception as e:
            self._log.exception("emit c-graduation proposal failed: %s", e)

    def _run_scheduled_c_agents(self, *, budget: "TickBudget",
                                  summary: dict) -> None:
        """Dispatch any C agents whose cadence has elapsed. Each
        invocation reuses the B-agent dispatcher under a
        target='c-agent:<name>' label so audit + rating semantics
        carry over.

        C invocations bypass the per-tick LLM budget gating only if
        explicitly opted in via cfg (default: respect budget). Each
        C costs ~$0.02 per run; with daily cadence and a small
        number of Cs, total cost stays well under $1/day.
        """
        try:
            due = self._db.list_due_c_agents()
        except AttributeError:
            return  # old install
        if not due:
            return
        try:
            import b_agents as _b_agents
        except Exception as e:
            self._log.warning("b_agents import failed in C-run: %s", e)
            return

        ran = 0
        for c in due:
            if budget.exhausted():
                summary.setdefault("skipped_due_to_budget", []).append(
                    f"c_agent:{c['name']}")
                break
            # For the first C cycle, C runs the SAME logic as its B
            # parent. The b_agent_name in the registry must match
            # c['graduated_from_b_name']. If the parent B is gone
            # (e.g. removed from the registry), skip with a log.
            b_parent = c["graduated_from_b_name"]
            if b_parent not in _b_agents.B_AGENTS:
                self._log.warning(
                    "c_agent '%s' has no B parent '%s' in registry; "
                    "skipping scheduled run",
                    c["name"], b_parent)
                continue
            # The first C use case (theme_check) doesn't need
            # specific args — it's an overseer-side decision which
            # theme to audit. For scheduled-without-args Cs we skip
            # for now and surface a notification asking overseer to
            # supply args. Future: store args_json on c_agents row.
            self._log.info(
                "c_agent '%s' is due but has no scheduled-args "
                "wiring yet; passing this slice", c["name"])
            # Mark last_run_at to avoid infinite due-polling. Even
            # though we didn't actually fire, the cadence is the
            # "check interval" semantically.
            try:
                self._db.update_c_agent_run(
                    c_agent_id=c["id"],
                    sibling_task_id=0,  # 0 = no actual run
                )
            except Exception:
                pass
            ran += 1

        if ran:
            summary["c_agents_due_handled"] = ran

    def _run_temporal_cadence(self, *, budget: TickBudget,
                                summary: dict):
        """Daily / weekly / monthly / yearly Sonnet rollups on a
        local-time schedule (22:00 local trigger window).

        Slice 5.6 — these BYPASS the daily LLM budget. They're
        time-anchored: missing the trigger window is permanent (the
        period closes, the moment is gone). Total spend across all
        4 kinds in a year is bounded (~365 daily × $0.01 + 52 weekly
        × $0.02 + 12 monthly × $0.01 + 1 yearly × $0.02 ≈ $5/year)
        — the per-call cost cap stays in force as the safety bound.
        Cost is still logged to llm_calls for full audit; the tick
        log records temporal_bypassed_budget=True when fired.

        UNIQUE(kind, period_label) on the table protects against
        double-generation across ticks within a trigger window.

        Per-tick at most ONE narrative kind is generated (highest
        priority eligible). This keeps a single tick's wall-clock
        time bounded; the next tick (15min later) picks up the next
        eligible kind.
        """
        local_now = T_clock.now_local()
        max_cost = float(self._cfg.get(
            "temporal_max_cost_usd_per_call",
            temporal_narrative.DEFAULT_MAX_COST_USD_PER_CALL,
        ))

        # Bypass-budget: a TickBudget with no daily-budget back-
        # propagation. Charges go nowhere (so the regular daily cap
        # for non-temporal work is unaffected); per-call cost cap
        # still applies via the max_cost_usd we pass to generate_*.
        bypass_budget = TickBudget(
            max_calls=10, max_cost_usd=1.0, daily_budget=None,
        )

        # Order of preference within a tick: yearly (rarest, Jan 1
        # only), then weekly (Sundays), then daily, then monthly
        # (1st of month). Yearly + monthly + weekly + daily can ALL
        # be eligible on Jan 1 if it's Sunday — they fire across
        # 4 ticks (~60min total) per the "one kind per tick" rule.
        # Build the candidate list. For each kind we include the
        # CURRENT period if its 22:00-local trigger has passed, AND
        # several PRIOR periods (catchup) — the dedup gate at
        # _maybe_generate_temporal_bounded handles 'already generated'
        # so catchup is a no-op when caught up.
        #
        # Catchup is the fix for the 'missed trigger window' failure
        # mode: previously a single missed tick at 22:00-23:59 meant
        # the daily for that day could NEVER fire automatically,
        # because once midnight passed the trigger predicate stopped
        # matching and the period rolled.
        from datetime import datetime, time, timedelta
        tz = local_now.tzinfo
        candidates = []  # (kind, period_label, period_start, period_end)

        # YEARLY: most recent Jan-1-ended year + 1 prior (catchup is
        # small — only ever one yearly to backfill since Slice 5.6).
        for years_back in range(0, 2):
            review_year = local_now.year - 1 - years_back
            jan1_22 = datetime(review_year + 1, 1, 1, 22, 0, tzinfo=tz)
            if local_now < jan1_22:
                continue
            ps_dt = datetime(review_year, 1, 1, 0, 0, tzinfo=tz)
            pe_dt = datetime(review_year + 1, 1, 1, 0, 0, tzinfo=tz)
            candidates.append((
                "yearly", str(review_year),
                T_clock.format_utc_iso(ps_dt),
                T_clock.format_utc_iso(pe_dt),
            ))

        # WEEKLY: this week (if past Sun 22:00) + 4 weeks back
        for weeks_back in range(0, 5):
            ref_dt = local_now - timedelta(weeks=weeks_back)
            ref_iso_dow = ref_dt.isoweekday()
            days_to_sun = (7 - ref_iso_dow) % 7
            sun_date = ref_dt.date() + timedelta(days=days_to_sun)
            sun_22 = datetime.combine(sun_date, time(22, 0), tzinfo=tz)
            if local_now < sun_22:
                continue
            mon_date = sun_date - timedelta(days=6)
            ps_dt = datetime.combine(mon_date, time.min, tzinfo=tz)
            pe_dt = datetime.combine(
                sun_date + timedelta(days=1), time.min, tzinfo=tz)
            iso_year, iso_week, _ = sun_date.isocalendar()
            label = "{}-W{:02d}".format(iso_year, iso_week)
            candidates.append((
                "weekly", label,
                T_clock.format_utc_iso(ps_dt),
                T_clock.format_utc_iso(pe_dt),
            ))

        # DAILY: today (if past 22:00) + 6 days back
        for days_back in range(0, 7):
            target = local_now - timedelta(days=days_back)
            target_22 = datetime.combine(
                target.date(), time(22, 0), tzinfo=tz)
            if local_now < target_22:
                continue
            ps_dt = datetime.combine(target.date(), time.min, tzinfo=tz)
            pe_dt = ps_dt + timedelta(days=1)
            candidates.append((
                "daily", target.strftime("%Y-%m-%d"),
                T_clock.format_utc_iso(ps_dt),
                T_clock.format_utc_iso(pe_dt),
            ))

        # MONTHLY: each month-just-ended where the 1st-22:00 trigger
        # has passed (most recent + 3 back).
        for months_back in range(0, 4):
            anchor = local_now.replace(day=1)
            for _ in range(months_back + 1):
                anchor = (anchor - timedelta(days=1)).replace(day=1)
            if anchor.month == 12:
                month_after_next = anchor.replace(
                    year=anchor.year + 1, month=1)
            else:
                month_after_next = anchor.replace(
                    month=anchor.month + 1)
            trigger_dt = datetime.combine(
                month_after_next.date(), time(22, 0), tzinfo=tz)
            if local_now < trigger_dt:
                continue
            ps_dt = datetime.combine(anchor.date(), time.min, tzinfo=tz)
            pe_dt = datetime.combine(
                month_after_next.date(), time.min, tzinfo=tz)
            candidates.append((
                "monthly", anchor.strftime("%Y-%m"),
                T_clock.format_utc_iso(ps_dt),
                T_clock.format_utc_iso(pe_dt),
            ))

        # Order: yearly -> weekly -> daily -> monthly (kind priority
        # preserved from Slice 5.6). Within a kind, most-recent first
        # (lexically larger period_label).
        kind_priority = {
            "yearly": 0, "weekly": 1, "daily": 2, "monthly": 3,
        }
        candidates.sort(
            key=lambda c: (kind_priority[c[0]], c[1]),
            reverse=False,
        )
        # Reverse within each kind by re-sorting on label desc:
        candidates.sort(
            key=lambda c: (kind_priority[c[0]], -1),
        )
        # Easiest correct sort: build groups, sort each group, then flatten.
        from itertools import groupby
        grouped = []
        for k, group in groupby(
            sorted(candidates, key=lambda c: kind_priority[c[0]]),
            key=lambda c: c[0],
        ):
            grouped.extend(sorted(list(group), key=lambda c: c[1], reverse=True))
        candidates = grouped

        for kind, period_label, period_start, period_end in candidates:
            ran = self._maybe_generate_temporal_bounded(
                kind=kind,
                period_label=period_label,
                period_start=period_start,
                period_end=period_end,
                local_now=local_now,
                max_cost=max_cost,
                budget=bypass_budget,
                summary=summary,
            )
            if ran:
                summary["temporal_bypassed_budget"] = True
                # One narrative per tick — the next tick (15min later)
                # picks up the next eligible candidate.
                break

    def _maybe_generate_temporal_bounded(self, *, kind, period_label,
                                          period_start, period_end,
                                          local_now, max_cost,
                                          budget, summary):
        """Generate one temporal narrative with explicit bounds.
        Used by the catchup pass so the loop can target prior
        periods (not just the period containing local_now)."""
        # Dedup gate
        existing = self._db.get_temporal_narrative(kind, period_label)
        if existing is not None:
            return False
        # Per-kind extra gates (preserved from _maybe_generate_temporal)
        if kind == "monthly":
            should, reason = temporal_narrative.monthly_should_run(
                self._db)
            if not should:
                self._log.info("monthly skipped: %s", reason)
                return False
        if kind == "yearly":
            should, reason = temporal_narrative.yearly_should_run(
                self._db, period_start, period_end)
            if not should:
                self._log.info("yearly skipped: %s", reason)
                return False
        gen_fn = {
            "daily":   temporal_narrative.generate_daily,
            "weekly":  temporal_narrative.generate_weekly,
            "monthly": temporal_narrative.generate_monthly,
            "yearly":  temporal_narrative.generate_yearly,
        }[kind]
        try:
            self._log.info(
                "generating temporal '%s' for %s", kind, period_label)
            result = gen_fn(
                db=self._db, llm=self._llm,
                period_start=period_start,
                period_end=period_end,
                period_label=period_label,
                local_now=local_now,
                max_cost_usd=max_cost,
                triggered_by="loop",
            )
        except Exception as e:
            self._log.exception(
                "temporal-%s generation crashed: %s", kind, e)
            summary["errors"].append(
                "temporal_{}: {}".format(kind, str(e)[:140]))
            return False
        cost = float(result.get("cost_usd") or 0.0)
        try:
            budget.charge({
                "ok": True, "cost_usd": cost,
                "prompt_tokens": result.get("prompt_tokens", 0),
                "completion_tokens": result.get("completion_tokens", 0),
            })
        except Exception:
            pass
        try:
            self._db.add_temporal_narrative(
                kind=kind,
                period_label=period_label,
                period_start=period_start,
                period_end=period_end,
                narrative=result.get("narrative", ""),
                cost_usd=cost,
                model=result.get("model", ""),
                triggered_by="loop",
            )
        except Exception as e:
            self._log.exception(
                "insert_temporal_narrative failed: %s", e)
            summary["errors"].append(
                "temporal_insert: " + str(e)[:140])
            return False
        summary.setdefault("temporal_generated", []).append({
            "kind": kind, "period_label": period_label,
            "cost_usd": cost,
        })
        return True

    def _maybe_generate_temporal(self, *, kind, local_now, max_cost,
                                  budget, summary):
        """Returns True if we actually generated, False if we skipped
        (dedup hit, monthly gate failed, etc.)."""
        # Compute period bounds + label per kind.
        if kind == "daily":
            # The day that's about to end — fires at 22:00 local,
            # covers TODAY (the day still in progress).
            period_start, period_end, period_label = (
                T_clock.today_local_bounds(local_now))
        elif kind == "weekly":
            # Mon→Sun ISO week containing local_now (when fired Sun
            # 22:00, that's the just-finished week).
            period_start, period_end, period_label = (
                T_clock.week_local_bounds(local_now))
        elif kind == "monthly":
            # On the 1st: review the month that just ENDED, not the
            # near-empty current month.
            period_start, period_end, period_label = (
                T_clock.previous_month_local_bounds(local_now))
        elif kind == "yearly":
            # On Jan 1: review the year that just ENDED.
            period_start, period_end, period_label = (
                T_clock.previous_year_local_bounds(local_now))
        else:
            return False

        # Dedup gate
        existing = self._db.get_temporal_narrative(kind, period_label)
        if existing is not None:
            return False

        # Monthly extra gate
        if kind == "monthly":
            should, reason = temporal_narrative.monthly_should_run(
                self._db)
            if not should:
                self._log.info("monthly skipped: %s", reason)
                return False

        # Yearly extra gate
        if kind == "yearly":
            should, reason = temporal_narrative.yearly_should_run(
                self._db, period_start, period_end)
            if not should:
                self._log.info("yearly skipped: %s", reason)
                return False

        gen_fn = {
            "daily":   temporal_narrative.generate_daily,
            "weekly":  temporal_narrative.generate_weekly,
            "monthly": temporal_narrative.generate_monthly,
            "yearly":  temporal_narrative.generate_yearly,
        }[kind]

        try:
            self._log.info("generating temporal '%s' for %s",
                           kind, period_label)
            result = gen_fn(
                db=self._db, llm=self._llm,
                period_start=period_start,
                period_end=period_end,
                period_label=period_label,
                local_now=local_now,
                max_cost_usd=max_cost,
                triggered_by="loop",
            )
        except Exception as e:
            self._log.exception(
                "temporal-%s generation crashed: %s", kind, e)
            summary["errors"].append("temporal_{}: {}".format(
                kind, str(e)[:140]))
            return False

        cost = float(result.get("cost_usd") or 0.0)
        try:
            budget.charge({
                "ok": True, "cost_usd": cost,
                "prompt_tokens": result.get("prompt_tokens", 0),
                "completion_tokens": result.get("completion_tokens", 0),
            })
        except Exception:
            pass

        if not result.get("ok"):
            summary["errors"].append("temporal_{}: {}".format(
                kind, (result.get("error") or "")[:140]))
            return False

        new_id = temporal_narrative.apply_temporal_narrative(
            db=self._db, gen_result=result,
            period_start=period_start,
            period_end=period_end,
            period_label=period_label,
            local_created_at=T_clock.format_local_iso(local_now),
        )

        if new_id:
            summary["temporal_generated"] = summary.get(
                "temporal_generated", []) + [{
                    "kind": kind,
                    "period_label": period_label,
                    "cost_usd": round(cost, 4),
                }]
            return True
        # UNIQUE conflict (rare — only if a parallel tick beat us)
        return False

    # ── Step 3: build working_memory artifact ────────────────────

    def build_working_memory(self) -> dict:
        """Assemble the working_memory dict per locked design.

        Slice 3f.5 #2: QUESTION-CENTERED. The primary view is now
        `top_questions` — active open_questions with their recent
        evidence — followed by everything that supports them
        (projects, decisions, todos, themes, episodes, digest).

        Backwards-compat: `open_questions` is kept as a flat list
        alias so existing UI consumers don't break.

        No LLM call — pure aggregation over cortex.db (read-only) and
        overseer.db. Cached in overseer_state so Hub/MCP reads are
        zero-latency.
        """
        top_n = int(self._cfg.get("working_memory_top_projects", 5))
        recency = int(self._cfg.get(
            "working_memory_top_projects_recency_days", 30))
        decisions_n = int(self._cfg.get(
            "working_memory_recent_decisions", 10))
        todos_n = int(self._cfg.get("working_memory_open_todos", 20))
        questions_n = int(self._cfg.get(
            "working_memory_open_questions", 10))
        themes_n = int(self._cfg.get("working_memory_recent_themes", 5))
        episode_titles_n = int(self._cfg.get(
            "working_memory_recent_episode_titles", 10))
        digest_days = int(self._cfg.get(
            "working_memory_last_week_days", 7))
        question_evidence_n = int(self._cfg.get(
            "working_memory_evidence_per_question", 4))
        unfiled_n = int(self._cfg.get(
            "working_memory_unfiled_recent_gists", 8))
        # Slice 3g — depth: patterns, drift, future-notes, rollups
        patterns_n = int(self._cfg.get(
            "working_memory_recent_patterns", 5))
        drift_n = int(self._cfg.get(
            "working_memory_recent_drift", 5))
        future_notes_n = int(self._cfg.get(
            "working_memory_recent_future_notes", 3))
        rollups_n = int(self._cfg.get(
            "working_memory_recent_rollups", 5))

        # ── PRIMARY: questions with recent evidence ──────────
        # Slice 3f.5 #2: questions are the primary organizing axis.
        top_questions = self._db.top_questions_with_evidence(
            limit=questions_n, recent_n=question_evidence_n,
        )
        # 3g #2: sprinkle drill-down tokens — both on the question itself
        # and on each evidence row that has a routable (table, id) pair.
        for q in top_questions:
            q["token"] = make_token("open_questions", q.get("id"))
            for ev in (q.get("recent_evidence") or []):
                ev["token"] = make_token(
                    ev.get("evidence_table"), ev.get("evidence_id"),
                )

        # Backwards-compat flat list (existing Hub UI reads
        # working_memory.open_questions). Strip evidence to match the
        # old shape; the rich version is at top_questions.
        legacy_open_questions = []
        for q in top_questions:
            legacy_open_questions.append({
                "id": q.get("id"),
                "question": q.get("question"),
                "confidence": q.get("confidence"),
                "tags": q.get("tags") or [],
            })

        # ── Supporting context ───────────────────────────────
        top_projects = self._core.query(
            "SELECT tag, name, status, priority, last_touched, "
            "total_hours, description, category "
            "FROM projects "
            "WHERE status='active' AND last_touched >= datetime('now', ?) "
            "ORDER BY last_touched DESC LIMIT ?",
            ("-{} days".format(recency), top_n),
        )

        themes = self._db.recent_themes(themes_n)
        for t in themes:
            t["tags"] = self._db.get_tags_for("summaries_theme", t["id"])
            t["token"] = make_token("summaries_theme", t["id"])

        episode_titles = [
            e["title"] for e in self._db.recent_episodes(episode_titles_n)
        ]

        # Last-week digest = recent gists from the window, joined chronological
        all_gists = self._db.recent_gists(50)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=digest_days)).strftime("%Y-%m-%d %H:%M:%S")
        recent_gists = [g for g in all_gists if (g["created_at"] or "") >= cutoff]
        recent_gists.reverse()  # chronological
        digest = " ".join(g["body"] for g in recent_gists)[:2000]

        # Unfiled recent gists — Tory's locked design says some events
        # legitimately don't connect to existing questions, and that's
        # itself signal: the unfiled set may be where new questions
        # are forming.
        unfiled = self._db.unfiled_recent_gists(limit=unfiled_n)
        for u in unfiled:
            u["token"] = make_token("summaries_gist", u.get("id"))

        # Slice 3f.5 #4: surface globally-applicable blindspots (ones
        # whose model_pattern matches Opus, the default summarizer)
        # at the top level of working memory. Per-question blindspots
        # come back in question_with_evidence's topic context (3i).
        try:
            global_blindspots = applicable_blindspots(
                db=self._db, model="anthropic/claude-opus-4.7",
                topic="", record_application=False,
            )
        except Exception:
            global_blindspots = []
        # Trim payload — caller sees title + body, not the full row
        global_blindspots = [
            {
                "id": bs["id"],
                "token": make_token("known_blindspots", bs["id"]),
                "model_pattern": bs.get("model_pattern"),
                "topic_pattern": bs.get("topic_pattern"),
                "direction": bs.get("direction"),
                "confidence_adjustment": bs.get("confidence_adjustment"),
                "body": bs.get("body"),
                "confidence": bs.get("confidence"),
            }
            for bs in global_blindspots[:8]
        ]

        # ── Slice 3g: depth — patterns, drift, future-notes, rollups ─
        # All four are zero-LLM aggregations over existing tables; they
        # surface signal that already lives in the DB but until now was
        # only reachable through deliberate UI clicks. The working_memory
        # artifact is the surface most callers (chat overseer, MCP
        # cortex_get_context, Hub Overview) read on every request, so
        # putting these here is what makes the overseer feel "deep"
        # instead of just "summarized".

        all_patterns = self._db.recent_patterns(limit=patterns_n)
        recent_patterns = [
            {
                "id": p["id"],
                "token": make_token("patterns", p["id"]),
                "name": p.get("name") or "",
                "body": _truncate(p.get("body") or "", 200),
                "confidence": p.get("confidence"),
                "occurrences": p.get("occurrences"),
                "last_observed_at": p.get("last_observed_at"),
            }
            for p in all_patterns
        ]

        all_drift = self._db.recent_drift(limit=drift_n)
        recent_drift = [
            {
                "id": d["id"],
                "token": make_token("drift_observations", d["id"]),
                "body": _truncate(d.get("body") or "", 200),
                "direction": d.get("direction") or "",
                "confidence": d.get("confidence"),
                "observed_at": d.get("observed_at"),
            }
            for d in all_drift
        ]

        # all_future_notes() is oldest-first (as accreted). For the
        # working memory excerpt we want the NEWEST notes — that's
        # what a future instance would care most about.
        all_future = self._db.all_future_notes()
        future_total = len(all_future)
        recent_future_notes = [
            {
                "id": n["id"],
                "token": make_token("future_overseer_notes", n["id"]),
                "instance_id": n.get("instance_id") or "",
                "written_at": n.get("written_at"),
                "body": _truncate(n.get("body") or "", 300),
            }
            for n in all_future[-future_notes_n:][::-1]  # newest first
        ]

        all_rollups = self._db.list_rollups(limit=rollups_n)
        recent_rollups = [
            {
                "id": r.get("id"),
                "token": make_token("automation_rollups", r.get("id")),
                "project": r.get("project"),
                "rollup_date": r.get("rollup_date"),
                "session_count": r.get("session_count"),
                "total_minutes": r.get("total_minutes"),
                "median_minutes": r.get("median_minutes"),
                "summary": _truncate(r.get("summary") or "", 300),
            }
            for r in all_rollups
        ]

        # ── Slice 9.2 (overseer ask #2): staleness signals ─────
        # The overseer asked to see its own ingest backlog + last-gist
        # freshness so it can tell whether a quiet stretch in top_projects
        # last_touched dates reflects user absence or just an unprocessed
        # ingest backlog. Round 3 added gist_source_distribution so the
        # overseer can self-detect sampling bias when one source's
        # rollups dominate its recent-gists view.
        import_queue = self._db.imported_sessions_queue_stats()
        last_gist_at = self._db.last_successful_gist_at()
        gist_dist = self._db.recent_gist_source_distribution(recent_n=30)

        # Slice 9.2.1 (2026-05-16): A-only sibling dispatch counters
        # in the freshness block. Per overseer's explicit ask: numbers
        # only — no "suggested next action" field, no nudges, no
        # placeholders for B/C agents that don't exist yet. The
        # overseer wants to "feel the absence of B telemetry" before
        # specifying it. See memory/agent_ecosystem_design.md.
        sibling_daily_cap = int(self._cfg.get(
            "loop_daily_sibling_dispatches", 20))

        # Build_working_memory imports git_ingest lazily so deployments
        # without the wrapper file don't import-fail. Inlined here so
        # the return dict (below) reads a local variable, not a method.
        try:
            import git_ingest as _gi
            git_ingest_state = _gi.last_run_state(self._db)
        except Exception:
            git_ingest_state = {}
        try:
            sibling_stats = self._db.sibling_dispatch_stats(
                daily_cap=sibling_daily_cap)
        except Exception as e:
            # Defensive: if sibling_tasks table is missing on an
            # older overseer.db (pre-9.3 migration), don't crash the
            # working-memory build. Surface zeros instead.
            self._log.warning(
                "sibling_dispatch_stats failed (table missing?): %s", e)
            sibling_stats = {
                "today_dispatches": 0,
                "daily_cap": sibling_daily_cap,
                "unrated_count": 0,
                "pending_for_me": 0,
            }

        # Slice 9.4.1 (2026-05-16): always emit BOTH UTC and local-
        # with-offset timestamps so any display surface can pick the
        # correct frame. See memory/feedback_time_always_local_with_tz.md.
        try:
            from temporal import format_local_iso as _fmt_local_iso
            _local_built_at = _fmt_local_iso()
        except Exception:
            _local_built_at = ""
        return {
            "built_at": _utc_iso(),
            "local_built_at": _local_built_at,
            "schema_version": 8,  # 9.4.1: +local_built_at
            "top_questions": top_questions,            # PRIMARY (3f.5)
            "top_projects": top_projects,
            "recent_decisions": self._core.recent_decisions(limit=decisions_n),
            "open_todos": self._core.open_reminders(limit=todos_n),
            "open_questions": legacy_open_questions,    # back-compat
            "recent_themes": themes,
            "recent_episode_titles": episode_titles,
            "last_week_digest": digest,
            "unfiled_recent_gists": unfiled,            # 3f.5: unfiled signal
            "future_overseer_notes_count": future_total,
            "journal_entry_count": self._db.journal_count(),
            "blindspots": global_blindspots,            # 3f.5 #4
            # 3g: depth signals
            "recent_patterns": recent_patterns,
            "recent_drift": recent_drift,
            "recent_future_notes": recent_future_notes,
            "recent_rollups": recent_rollups,
            # 9.2 #2: staleness signals (the overseer's self-awareness)
            "import_queue_depth": import_queue["total"],
            "import_queue_by_source": import_queue["by_source"],
            "last_successful_gist_at": last_gist_at,
            # 9.2 round 3: gist-source distribution so the overseer can
            # detect when its recent-themes view is fitted to a biased
            # slice (e.g. all chatgpt-archive rollups, no grok yet).
            "recent_gist_source_distribution": gist_dist,
            # 9.2.1: sibling dispatch posture (A-only). today_dispatches
            # is the cap counter; unrated_count is completed-but-not-
            # rated-by-overseer; pending_for_me is dispatched-but-
            # not-yet-returned. Rendered in chat freshness section.
            "sibling_dispatched_today": sibling_stats["today_dispatches"],
            "sibling_daily_cap": sibling_stats["daily_cap"],
            "sibling_unrated_count": sibling_stats["unrated_count"],
            "sibling_pending_for_me": sibling_stats["pending_for_me"],
            # 9.4 CP2: git ingest state (when this channel last refreshed,
            # what was attempted, what was skipped). Lets the freshness
            # block answer "is the git channel current?" and "what am I
            # NOT seeing?" — overseer's explicit caveat on the slice.
            "git_ingest": git_ingest_state,
            # 9.6 CP3: unread notification responses from Tory. Bell
            # tab is now a two-way channel; this is the count of
            # action-button clicks / free-text replies overseer hasn't
            # yet read+acted-on. Surfaced in freshness so overseer
            # notices when Tory has actually responded.
            "pending_notification_responses": (
                self._db.pending_notification_responses_count()
                if hasattr(self._db, "pending_notification_responses_count")
                else 0),
        }

    # ── Backfill (manual) ────────────────────────────────────────

    def backfill(self, *, kind: str = "all", session_limit: int = 200,
                 note_limit: int = 500,
                 max_cost_usd: float = 1.0,
                 max_calls: int | None = None) -> dict:
        """Process ALL unprocessed historical sessions/notes (rate-limited).

        Triggered by POST /plugins/overseer/backfill. Default budget is
        more generous than a tick ($1.00 vs $0.50) but still capped so the
        user can't accidentally burn through their balance. Caller can
        bump max_cost_usd / max_calls in the request body.

        kind: "sessions" | "notes" | "all"
        """
        if not self._tick_lock.acquire(blocking=False):
            return {"ok": False,
                    "skipped": "a tick or backfill is already running"}
        try:
            return self._backfill_locked(
                kind=kind, session_limit=session_limit,
                note_limit=note_limit, max_cost_usd=max_cost_usd,
                max_calls=max_calls,
            )
        finally:
            self._tick_lock.release()

    def _backfill_locked(self, *, kind, session_limit, note_limit,
                         max_cost_usd, max_calls) -> dict:
        if max_calls is None:
            # Heuristic: 1 call per session + ~1 per 8 notes + slack
            max_calls = int(session_limit + (note_limit / 8) + 5)
        budget = TickBudget(
            max_calls=int(max_calls),
            max_cost_usd=float(max_cost_usd),
        )
        summary = {
            "ok": True, "kind": kind,
            "started_at": _utc_iso(),
            "sessions_summarized": 0, "sessions_failed": 0,
            "sessions_empty": 0,
            "notes_tagged": 0, "notes_failed": 0,
            "skipped_due_to_budget": [],
            "errors": [],
        }

        if kind in ("all", "sessions"):
            try:
                self._backfill_sessions(budget, summary, session_limit)
            except Exception as e:
                self._log.exception("backfill sessions failed: %s", e)
                summary["errors"].append(
                    "backfill_sessions: " + str(e)[:200])

        if kind in ("all", "imports") and not budget.exhausted():
            try:
                self._summarize_imported_sessions(budget, summary)
            except Exception as e:
                self._log.exception("backfill imports failed: %s", e)
                summary["errors"].append(
                    "backfill_imports: " + str(e)[:200])

        # Slice 3e backfill: classification + rollups for the existing
        # historical UFOSINT-class backlog. Bypasses per-tick rollup cap.
        if kind in ("all", "classify", "rollups") and not budget.exhausted():
            try:
                changes = self._db.auto_classify_projects()
                summary["classify_changed"] = sum(
                    1 for c in changes if c.get("changed_to"))
                summary["classify_results"] = changes[:30]
            except Exception as e:
                self._log.exception("backfill classify failed: %s", e)
                summary["errors"].append("classify: " + str(e)[:200])

        if kind in ("all", "rollups") and not budget.exhausted():
            try:
                self._backfill_all_rollups(budget, summary)
            except Exception as e:
                self._log.exception("backfill rollups failed: %s", e)
                summary["errors"].append("backfill_rollups: " + str(e)[:200])

        if kind in ("all", "notes") and not budget.exhausted():
            try:
                self._backfill_notes(budget, summary, note_limit)
            except Exception as e:
                self._log.exception("backfill notes failed: %s", e)
                summary["errors"].append(
                    "backfill_notes: " + str(e)[:200])

        # Always rebuild working memory after a backfill
        try:
            wm = self.build_working_memory()
            self._db.set_overseer_state("working_memory_json", json.dumps(wm))
            self._db.set_overseer_state("working_memory_built_at", _utc_iso())
            summary["working_memory_rebuilt"] = True
        except Exception as e:
            summary["errors"].append("working_memory: " + str(e)[:200])

        summary["finished_at"] = _utc_iso()
        summary["budget"] = budget.remaining()
        return summary

    def _backfill_sessions(self, budget, summary, limit):
        rows = self._core.query(
            "SELECT id, ai_platform, hostname, started_at, ended_at, "
            "summary, projects FROM sessions "
            "WHERE ended_at IS NOT NULL ORDER BY started_at ASC LIMIT ?",
            (int(limit),),
        )
        for s in rows:
            if budget.exhausted():
                summary["skipped_due_to_budget"].append(
                    "session:" + s["id"])
                break
            if self._db.is_session_processed(s["id"]):
                continue
            outcome = self._summarize_one_session(s, budget, summary)
            if outcome == "summarized":
                summary["sessions_summarized"] += 1
            elif outcome == "empty":
                summary["sessions_empty"] += 1
            else:
                summary["sessions_failed"] += 1

    def _backfill_all_rollups(self, budget, summary):
        """Run rollups for every (automation_project, date) combo not
        yet rolled up. Bypasses the per-tick cap — bounded only by the
        backfill budget passed in."""
        rows = self._db._conn.execute(
            "SELECT project FROM imported_project_settings "
            "WHERE treat_as = 'automation'"
        ).fetchall()
        n = 0
        for r in rows:
            project = r[0]
            for d in self._db.imports_dates_for_project(project):
                if budget.exhausted():
                    summary["skipped_due_to_budget"].append(
                        "rollup:{}:{}".format(project, d))
                    return
                if self._db.get_rollup(project, d):
                    continue
                try:
                    res = generate_rollup(
                        db=self._db, llm=self._llm,
                        project=project, rollup_date=d, budget=budget)
                    if res.get("ok"):
                        n += 1
                        summary.setdefault("rollups_generated", 0)
                        summary["rollups_generated"] += 1
                        if res.get("anomaly"):
                            summary.setdefault("rollups_anomalies", 0)
                            summary["rollups_anomalies"] += 1
                except Exception as e:
                    self._log.warning(
                        "rollup %s/%s failed during backfill: %s",
                        project, d, e)

    def _backfill_notes(self, budget, summary, limit):
        rows = self._core.query(
            "SELECT id, content, tags, project, note_type, source, "
            "session_id, created_at FROM notes "
            "WHERE (tags IS NULL OR tags = '') "
            "ORDER BY created_at ASC LIMIT ?",
            (int(limit),),
        )
        unprocessed = [n for n in rows
                       if not self._db.is_note_processed(n["id"])]
        if not unprocessed:
            return

        batch_size = int(self._cfg.get("loop_tag_batch_size", 10))
        max_per_note = int(self._cfg.get("loop_tag_max_per_note", 3))
        for i in range(0, len(unprocessed), batch_size):
            if budget.exhausted():
                summary["skipped_due_to_budget"].append(
                    "notes-batch@{}".format(i))
                break
            batch = unprocessed[i:i + batch_size]
            tagged_n, failed_n = self._tag_one_batch(
                batch, budget, max_per_note)
            summary["notes_tagged"] += tagged_n
            summary["notes_failed"] += failed_n


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(s: str, n: int) -> str:
    if not s or len(s) <= n:
        return s or ""
    return s[:n].rstrip() + "…"
