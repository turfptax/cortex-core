"""Cortex overseer plugin — slice 3b.

Memory upkeep agent. Replaces the runtime's _NullLLMRouter and
_NullCoreMemoryRO with real implementations, owns overseer.db (drop-and-
rebuild safe), seeds it from the bundled Session 0 artifact, and exposes
a small HTTP surface for manual testing.

Locked design — see overseer_design.md and session_0_artifact.md.

on_load() ordering (mirrors the proven pet pattern):
  1. Open OverseerDB FIRST (creates plugin schema on overseer.db).
  2. Swap api.db so handlers see overseer schema.
  3. Wire CoreMemoryRO against cortex.db (read-only).
  4. Wire LLMRouter (three backends + fallback + logging).
  5. Seed Session 0 artifact (idempotent — safe to re-run).

Slice 3c will start the background consolidation loop; 3d adds Hub UI
and the cortex_get_context working_memory injection.
"""

from __future__ import annotations

import logging
import os
import time
import tomllib

import json
import shutil
from pathlib import Path

from plugin_api import Plugin, Route
from overseer_db import OverseerDB
from llm_router import LLMRouter
from core_memory_ro import CoreMemoryRO
from ingest_session_0 import ingest_seed
from loop import OverseerLoop
import corpus  # search_corpus + SEARCH_TARGETS (extracted 2026-05-27)
from claude_jsonl import (
    CLAUDE_CODE_SOURCE,
    canonicalize_project_name,
    claude_code_imported_id,
    claude_code_session_id_from_path,
    file_sha256,
    parse_claude_code_jsonl,
)
from chat import respond_to_message, respond_via_router
from dialectic import paired_generate, write_dialectic_row
from prompts import recent_notes_gist_prompt
from detail import resolve_detail, TokenError
from insight_scan import scan_project_arcs, apply_pending_interpretation
from distill_corrections import distill_uncondidated_corrections
import project_summary
import project_narrative
import temporal as temporal_clock
import temporal_narrative


log = logging.getLogger("plugin.overseer")


def _strip_meta(payload):
    """Drop framework metadata keys (those starting with __) from payload."""
    return {k: v for k, v in payload.items() if not k.startswith("__")}


def _as_int(payload, key, default, max_value=None):
    val = payload.get(key, default)
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = default
    if max_value is not None:
        n = min(n, max_value)
    return n


def _safe_json_loads(raw, fallback):
    """json.loads(raw) but never raises — returns `fallback` on
    None/'' or any decode error. Used by route handlers that
    deserialize TEXT-stored JSON columns before returning to clients."""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _safe_list(value):
    """Coerce a possibly-mixed value into a list[str]:
      - list of strings → as-is
      - comma-separated string → split + strip
      - None / '' → []
      - other → [str(value)]
    Used by people add/update routes so MCP agents can pass either
    'a, b, c' or ['a', 'b', 'c'] for handles/expertise/tags."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        return [p for p in parts if p]
    return [str(value)]


def _safe_list_or_none(value):
    """Like _safe_list but returns None when value was not provided
    (so update_person knows the caller didn't intend to touch the
    field). Empty list IS a valid 'clear' signal."""
    if value is None:
        return None
    return _safe_list(value)


def _parse_people_json(row):
    """Mutate a people row in place: parse the JSON-stored array
    columns into actual Python lists. Removes the *_json suffix on
    the keys for client convenience."""
    if not row:
        return row
    row["online_handles"] = _safe_json_loads(
        row.pop("online_handles_json", "[]"), [])
    row["social_links"] = _safe_json_loads(
        row.pop("social_links_json", "[]"), [])
    row["areas_of_expertise"] = _safe_json_loads(
        row.pop("areas_of_expertise_json", "[]"), [])
    row["tags"] = _safe_json_loads(row.pop("tags_json", "[]"), [])
    row["aliases"] = _safe_json_loads(row.pop("aliases_json", "[]"), [])
    return row


def _render_intro_markdown(brief: dict) -> str:
    """Render the /intro brief as a single markdown document. This is
    what an external AI reading at the top of a conversation actually
    wants — not nested JSON, but a 30-second human-friendly briefing.

    Format intentionally mirrors what Tory described in the
    2026-06-08 refactor: WHO IS TORY → WHAT HE'S WORKING ON → WHAT
    HE'S THINKING ABOUT → RECENT DECISIONS → THEMES → DRIFT →
    BLINDSPOTS → INSTITUTIONAL MEMORY → OPS (last).
    """
    parts: list[str] = []
    parts.append(f"# Tory — Context Brief")
    parts.append("")
    parts.append(f"*Generated {brief.get('generated_at','')}*")
    parts.append("")

    who = brief.get("who_is_tory") or {}
    if who:
        parts.append("## Who Tory is")
        parts.append("")
        for k, label in (
            ("full_name", "Name"),
            ("title", "Title"),
            ("employer", "Employer"),
            ("location", "Location"),
            ("neurotype", "Neurotype"),
        ):
            if who.get(k):
                parts.append(f"- **{label}:** {who[k]}")
        if who.get("working_style"):
            parts.append("")
            parts.append("### How to work with him")
            parts.append("")
            for line in who["working_style"]:
                parts.append(f"- {line}")
        if who.get("sensitive_topics"):
            parts.append("")
            parts.append("### Sensitive topics")
            parts.append("")
            for line in who["sensitive_topics"]:
                parts.append(f"- {line}")
        parts.append("")

    working_on = brief.get("working_on") or []
    if working_on:
        parts.append("## What he's working on")
        parts.append("")
        parts.append("| Project | Status | Sessions | Minutes | Last touched |")
        parts.append("|---|---|---:|---:|---|")
        for p in working_on:
            parts.append(
                f"| **{p['project']}** | {p['status']} | "
                f"{p['session_count']} | {p['minutes_total']} | "
                f"{p['last_active']} |"
            )
        parts.append("")
        # First project narrative if non-empty (the current focus)
        first = working_on[0]
        if first.get("narrative_excerpt"):
            parts.append(f"*Current focus context:* "
                          f"{first['narrative_excerpt']}")
            parts.append("")

    thinking = brief.get("thinking_about") or []
    if thinking:
        parts.append("## What he's thinking about (open questions)")
        parts.append("")
        for q in thinking:
            evidence = q.get("evidence_count", 0)
            parts.append(
                f"- *[{q['confidence']}]* **{q['question']}** "
                f"({evidence} evidence pieces, drill `{q['token']}`)"
            )
        parts.append("")

    decisions = brief.get("recent_decisions") or []
    if decisions:
        parts.append("## Recent decisions")
        parts.append("")
        for d in decisions[:10]:
            project = (f" *[{d['project']}]*" if d.get("project")
                        else "")
            drill = (f" — drill `{d['drill_token']}`"
                      if d.get("drill_token") else "")
            parts.append(
                f"- **{d['decided_on']}**{project} {d['decision']}"
                f"{drill}"
            )
        parts.append("")

    themes = brief.get("recent_themes") or []
    if themes:
        parts.append("## Recent themes")
        parts.append("")
        for t in themes:
            parts.append(
                f"- *[{t['confidence']}]* **{t['title']}** — "
                f"{t['claim']}"
            )
        parts.append("")

    drift = brief.get("key_drift") or []
    if drift:
        parts.append("## Key drift")
        parts.append("")
        for d in drift:
            direction = (f" ({d['direction']})"
                          if d.get("direction") else "")
            parts.append(
                f"- *[{d['confidence']}]*{direction} "
                f"{d['observation']}  *(observed "
                f"{d['observed_at']})*"
            )
        parts.append("")

    bs = brief.get("blindspots") or []
    if bs:
        parts.append("## Calibration notes for the AI reading this")
        parts.append("")
        for b in bs:
            parts.append(f"- {b['calibration_note']}")
        parts.append("")

    rules = brief.get("standing_tech_rules") or []
    if rules:
        parts.append("## Standing tech rules (apply these)")
        parts.append("")
        parts.append("*Hard-won defaults from things that actually "
                      "went wrong in his stacks. Full stories via the "
                      "cortex_rules tool.*")
        parts.append("")
        for r in rules:
            stack = f" `[{r['stack']}]`" if r.get("stack") else ""
            parts.append(f"- **{r['title']}**{stack}: {r['rule']}")
        parts.append("")

    notes = brief.get("recent_future_notes") or []
    if notes:
        parts.append("## Institutional memory (from prior instances)")
        parts.append("")
        for n in notes:
            parts.append(
                f"- *{n['written_at']}, {n['author']}:* "
                f"{n['excerpt']}"
            )
        parts.append("")

    ops = brief.get("ops") or {}
    if ops:
        parts.append("---")
        parts.append("## Operational state (overseer's plumbing)")
        parts.append("")
        parts.append("*Read only if you need to reason about the "
                      "Cortex system itself — most readers can skip.*")
        parts.append("")
        for k, v in ops.items():
            parts.append(f"- `{k}`: {v}")
        parts.append("")

    return "\n".join(parts)


class OverseerPlugin(Plugin):
    """Memory upkeep agent — slice 3b: real LLM + memory wiring + seed."""

    def __init__(self, api):
        super().__init__(api)
        self.overseer_db: OverseerDB | None = None
        self.llm: LLMRouter | None = None
        self.core_memory: CoreMemoryRO | None = None
        self.loop: OverseerLoop | None = None
        self._seed_summary: dict = {}

    # ── Context contribution (slice 3d-A) ───────────────────────

    def contribute_to_context(self) -> dict:
        """Inject working_memory into cortex_get_context's response.

        Returns the cached artifact from overseer.db — zero-latency, no
        LLM call. If the cache is empty (first boot before first tick),
        returns a status marker so the caller knows overseer is alive
        but warming up rather than missing entirely.

        Per locked design: ONE tool, two depths. The full artifact lives
        here; deeper drill-down (e.g. "expand episode E1's source") will
        come via a working_memory_detail_token in a future slice.
        """
        if self.overseer_db is None:
            return {
                "working_memory": None,
                "working_memory_status": "uninitialized",
            }
        cached = self.overseer_db.get_overseer_state("working_memory_json")
        built_at = self.overseer_db.get_overseer_state(
            "working_memory_built_at")
        if not cached:
            return {
                "working_memory": None,
                "working_memory_status": "warming-up",
                "working_memory_built_at": None,
                "working_memory_hint": (
                    "First overseer tick has not run yet. Call POST "
                    "/plugins/overseer/tick-now or wait "
                    "loop_first_tick_delay_s seconds."
                ),
            }
        try:
            wm = json.loads(cached)
        except Exception as e:
            return {
                "working_memory": None,
                "working_memory_status": "cache-corrupt",
                "working_memory_error": str(e)[:200],
            }
        # Slice 9.2 (overseer ask #2): compute the cache age at READ time
        # so the consumer doesn't have to. The overseer flagged that
        # it was confidently citing stale top_projects last_touched
        # without knowing how long ago the snapshot was built. With
        # working_memory_age_minutes in the static context, it can
        # gate its own confidence statements on freshness.
        age_minutes = None
        if built_at:
            try:
                from datetime import datetime, timezone
                b = datetime.fromisoformat(built_at.replace("Z", "+00:00"))
                age_minutes = max(
                    0,
                    int((datetime.now(timezone.utc) - b).total_seconds() / 60),
                )
            except Exception:
                age_minutes = None
        return {
            "working_memory": wm,
            "working_memory_built_at": built_at,
            "working_memory_age_minutes": age_minutes,
            "working_memory_status": "fresh",
        }

    # ── HTTP routes ─────────────────────────────────────────────

    def http_routes(self):
        return [
            Route("GET",  "/status",                self._http_status),
            Route("POST", "/ingest-session-0",      self._http_ingest_session_0),
            Route("GET",  "/seed",                  self._http_seed),
            Route("POST", "/llm/test",              self._http_llm_test),
            Route("GET",  "/llm/calls",             self._http_llm_calls),
            Route("GET",  "/llm/stats",             self._http_llm_stats),
            # Slice 14.6 CP1: per-model + per-purpose cost attribution.
            Route("GET",  "/llm/attribution",       self._http_llm_attribution),
            Route("POST", "/summarize-recent",      self._http_summarize_recent),
            Route("GET",  "/themes",                self._http_themes),
            Route("GET",  "/episodes",              self._http_episodes),
            Route("GET",  "/questions",             self._http_questions),
            Route("GET",  "/patterns",              self._http_patterns),
            Route("GET",  "/drift",                 self._http_drift),
            Route("GET",  "/future-notes",          self._http_future_notes),
            # ── Slice 3c: background loop + working memory ──────
            Route("GET",  "/loop",                  self._http_loop_status),
            Route("POST", "/tick-now",              self._http_tick_now),
            Route("POST", "/backfill",              self._http_backfill),
            Route("GET",  "/working-memory",        self._http_working_memory),
            # ── Slice 3d: Claude session import ─────────────────
            Route("POST", "/imports/from-path",     self._http_import_from_path),
            Route("POST", "/imports/scan-dir",      self._http_import_scan_dir),
            Route("GET",  "/imports",               self._http_list_imports),
            Route("POST", "/imports/delete",        self._http_delete_import),
            # ── Slice 3e: classification + rollups ──────────────
            Route("GET",  "/projects",              self._http_list_projects),
            Route("POST", "/projects/classify",     self._http_classify_now),
            Route("POST", "/projects/setting",      self._http_set_project_setting),
            Route("GET",  "/rollups",               self._http_list_rollups),
            # ── Slice 3e: chat ──────────────────────────────────
            Route("POST", "/chat",                  self._http_chat),
            # ── Slice 14.7 (2026-05-22): router-tier chat ───────
            Route("POST", "/quick-chat",            self._http_quick_chat),
            Route("GET",  "/chat/history",          self._http_chat_history),
            Route("POST", "/chat/clear",            self._http_chat_clear),
            Route("POST", "/chat/compress",         self._http_chat_compress),
            # ── Agent harness (2026-07-10): chat threads ─────────
            Route("GET",  "/chat/threads",          self._http_chat_threads),
            Route("POST", "/chat/threads/new",      self._http_chat_thread_new),
            Route("POST", "/chat/threads/select",   self._http_chat_thread_select),
            Route("POST", "/chat/threads/rename",   self._http_chat_thread_rename),
            Route("POST", "/chat/threads/delete",   self._http_chat_thread_delete),
            # ── Agent harness: prompt library ────────────────────
            Route("GET",  "/chat/prompts",          self._http_chat_prompts),
            Route("POST", "/chat/prompts/upsert",   self._http_chat_prompt_upsert),
            Route("POST", "/chat/prompts/delete",   self._http_chat_prompt_delete),
            # ── Agent harness (2026-07-11): harness map ──────────
            Route("GET",  "/harness-map",           self._http_harness_map),
            # ── Agent harness: interaction meta-feedback ─────────
            Route("GET",  "/feedback",              self._http_feedback_list),
            Route("POST", "/feedback",              self._http_feedback_add),
            Route("POST", "/feedback/discuss",      self._http_feedback_discuss),
            Route("GET",  "/feedback/summary",      self._http_feedback_summary),
            # ── Simples mirror (2026-07-11): phone plan snapshot ─
            Route("GET",  "/simples/snapshot",      self._http_simples_snapshot_get),
            Route("POST", "/simples/snapshot",      self._http_simples_snapshot_post),
            # ── Agent harness: MCP connectors (Pi as MCP client) ─
            Route("GET",  "/mcp/connectors",        self._http_mcp_connectors),
            Route("POST", "/mcp/connectors/upsert", self._http_mcp_connector_upsert),
            Route("POST", "/mcp/connectors/delete", self._http_mcp_connector_delete),
            Route("POST", "/mcp/connectors/test",   self._http_mcp_connector_test),
            # ── Slice 3e: notifications ─────────────────────────
            Route("GET",  "/notifications",         self._http_notifications),
            Route("POST", "/notifications/dismiss", self._http_notifications_dismiss),
            Route("POST", "/notifications/action",  self._http_notifications_action),
            Route("POST", "/notifications/respond", self._http_notifications_respond),
            # ── Slice 3e: budget visibility ─────────────────────
            Route("GET",  "/budget",                self._http_budget),
            # Slice 14.7.2 (2026-05-26): manual cap override.
            Route("POST", "/budget/override",       self._http_budget_override),
            # ── Slice 3f: dialectic checker ─────────────────────
            Route("GET",  "/dialectic",             self._http_list_dialectic),
            Route("GET",  "/dialectic/get",         self._http_get_dialectic),
            Route("POST", "/dialectic/resolve",     self._http_resolve_dialectic),
            Route("GET",  "/dialectic/counts",      self._http_dialectic_counts),
            # ── Slice 3f.5: overseer journal ────────────────────
            Route("GET",  "/journal",               self._http_journal),
            Route("POST", "/journal/reflect-now",   self._http_journal_reflect_now),
            # ── Slice 3f.5 #2: question-centered ─────────────────
            # /questions GET already exists; these augment.
            Route("GET",  "/questions/get",         self._http_question_detail),
            Route("POST", "/questions/lifecycle",   self._http_question_lifecycle),
            Route("POST", "/questions/upsert",      self._http_question_upsert),
            Route("POST", "/questions/route-existing", self._http_route_existing_gists),
            # ── Slice 3f.5 #4: known blindspots ──────────────────
            Route("GET",  "/blindspots",            self._http_list_blindspots),
            Route("POST", "/blindspots/upsert",     self._http_upsert_blindspot),
            Route("POST", "/blindspots/active",     self._http_blindspot_active),
            Route("POST", "/corrections",           self._http_log_correction),
            Route("GET",  "/corrections",           self._http_list_corrections),
            # ── Slice 3g checkpoint 2: drill-down ────────────────
            Route("GET",  "/detail",                self._http_detail),
            # ── Slice 3h: insight generation ─────────────────────
            Route("POST", "/insight/scan-now",      self._http_insight_scan_now),
            Route("GET",  "/insight/pending",       self._http_insight_pending),
            Route("POST", "/insight/decide",        self._http_insight_decide),
            Route("GET",  "/insight/scans",         self._http_insight_scans),
            # ── Slice 3i CP2: distill corrections → blindspots ──
            Route("POST", "/insight/distill-corrections",
                  self._http_distill_corrections),
            # ── Polish slice: Data Explorer graph ────────────────
            Route("GET",  "/explorer/graph",
                  self._http_explorer_graph),
            # ── Slice 4 CP1a: project rollup data layer ──────────
            Route("GET",  "/projects/summary",
                  self._http_list_project_summaries),
            Route("GET",  "/projects/summary/get",
                  self._http_get_project_summary),
            Route("POST", "/projects/summary/refresh",
                  self._http_refresh_project_summary),
            Route("POST", "/projects/summary/refresh-all",
                  self._http_refresh_all_project_summaries),
            # ── Slice 4 CP1b: project narrative ──────────────────
            Route("POST", "/narrative/generate",
                  self._http_generate_project_narrative),
            # ── Slice 5: temporal cadence ────────────────────────
            Route("GET",  "/temporal",
                  self._http_list_temporal),
            Route("GET",  "/temporal/get",
                  self._http_get_temporal),
            Route("POST", "/temporal/generate",
                  self._http_generate_temporal),
            Route("GET",  "/human-journal",
                  self._http_list_human_journal),
            Route("POST", "/human-journal",
                  self._http_add_human_journal),
            Route("POST", "/human-journal/delete",
                  self._http_delete_human_journal),
            # ── Slice 6: people ──────────────────────────────────
            Route("GET",  "/people",
                  self._http_list_people),
            Route("GET",  "/people/get",
                  self._http_get_person),
            Route("GET",  "/people/search",
                  self._http_search_people),
            Route("POST", "/people/add",
                  self._http_add_person),
            Route("POST", "/people/update",
                  self._http_update_person),
            Route("POST", "/people/merge",
                  self._http_people_merge),
            Route("POST", "/people/delete",
                  self._http_delete_person),
            Route("POST", "/people/link-project",
                  self._http_link_project_person),
            Route("POST", "/people/unlink-project",
                  self._http_unlink_project_person),
            Route("GET",  "/people/for-project",
                  self._http_people_for_project),
            Route("GET",  "/people/stats",
                  self._http_people_stats),
            # person_notes (2026-06-13 taxonomy build)
            Route("GET",  "/people/notes",
                  self._http_list_person_notes),
            Route("POST", "/people/notes/add",
                  self._http_add_person_note),
            Route("POST", "/people/notes/delete",
                  self._http_delete_person_note),
            # ── Day-in-Cortex (2026-07-12): permanent memory ─────
            Route("GET",  "/day",
                  self._http_day_detail),
            Route("GET",  "/day/heat",
                  self._http_day_heat),
            # ── Tech skills + rules (2026-07-12) ─────────────────
            Route("GET",  "/skills",
                  self._http_skills_list),
            Route("GET",  "/skills/get",
                  self._http_skills_get),
            Route("POST", "/skills/log",
                  self._http_skills_log),
            Route("GET",  "/rules",
                  self._http_rules_list),
            Route("POST", "/rules/add",
                  self._http_rules_add),
            # ── Slice 9.3: sibling task dispatch ─────────────────
            Route("POST", "/sibling/dispatch",
                  self._http_sibling_dispatch),
            Route("GET",  "/sibling/pending",
                  self._http_sibling_pending),
            Route("POST", "/sibling/claim",
                  self._http_sibling_claim),
            Route("POST", "/sibling/complete",
                  self._http_sibling_complete),
            Route("POST", "/sibling/reject",
                  self._http_sibling_reject),
            Route("GET",  "/sibling/recent",
                  self._http_sibling_recent),
            Route("GET",  "/sibling/stats",
                  self._http_sibling_stats),
            # ── Slice 10.4 (2026-05-20): ecosystem visualizer ────
            Route("GET",  "/ecosystem",
                  self._http_ecosystem),
            # ── Slice 10.4 Phase 2: runs / activity ───────────────
            Route("GET",  "/runs/recent",
                  self._http_runs_recent),
            Route("GET",  "/runs/detail",
                  self._http_runs_detail),
            Route("GET",  "/runs/export",
                  self._http_runs_export),
            Route("POST", "/runs/rate",
                  self._http_runs_rate),
            # ── Lemon Squeezer dispatch export (2026-06-13) ───────
            Route("GET",  "/dispatch-export",
                  self._http_dispatch_export),
            # ── Work-org (2026-05-21): targeted import processing ─
            Route("POST", "/imports/tag-machine",
                  self._http_imports_tag_machine),
            Route("POST", "/imports/process-targeted",
                  self._http_imports_process_targeted),
            # ── Slice 13 (2026-05-21): sensitivity tiers ──────────
            Route("GET",  "/sensitivity/status",
                  self._http_sensitivity_status),
            Route("POST", "/sensitivity/backfill",
                  self._http_sensitivity_backfill),
            # ── Slice 14.7.3 (2026-05-26): work/cortex/personal ──
            Route("GET",  "/category/status",
                  self._http_category_status),
            Route("POST", "/category/backfill-rules",
                  self._http_category_backfill_rules),
            Route("POST", "/category/classify-batch",
                  self._http_category_classify_batch),
            Route("POST", "/category/set",
                  self._http_category_set),
            # ── Phase 1 (2026-05-27): corpus search + pull events ─
            Route("GET",  "/search",
                  self._http_search_corpus),
            Route("GET",  "/pull-events",
                  self._http_recent_pull_events),
            Route("GET",  "/pull-events/stats",
                  self._http_pull_event_stats),
            # ── Phase 1 (2026-05-27): Claude Desktop import scaffold
            Route("POST", "/imports/claude-desktop/dry-run",
                  self._http_claude_desktop_dry_run),
            # ── Phase 2 (2026-05-27): vault generator (scaffold) ─
            Route("POST", "/vault/render",
                  self._http_vault_render),
            # ── Sub-agent tiers (2026-05-27): cost discipline ────
            Route("GET",  "/sub-agents",
                  self._http_list_sub_agents),
            Route("POST", "/sub-agents/set-tier",
                  self._http_set_sub_agent_tier),
            Route("GET",  "/sub-agents/performance",
                  self._http_sub_agent_performance),
            # ── Looper log (2026-06-05): /loop AI activity ───────
            Route("POST", "/looper/start",
                  self._http_looper_start),
            Route("POST", "/looper/finish",
                  self._http_looper_finish),
            Route("GET",  "/looper/recent",
                  self._http_looper_recent),
            # ── F1 coverage snapshot (2026-06-08, deterministic
            #    loop): single-call read of the abstraction-graph
            #    coverage metric the looper pushed in cycle 2.
            Route("GET",  "/f1-coverage",
                  self._http_f1_coverage),
            # ── Context brief (2026-06-08): the new "first 30
            #    seconds tells you Tory" surface for external AIs.
            #    Demotes overseer's operational chatter; leads with
            #    who, what's-being-worked-on, what's-thought-about.
            Route("GET",  "/intro",
                  self._http_intro),
            # ── Vector index (2026-06-10): local semantic search ──
            #    over the gist corpus. bge-small via llama-embed on
            #    :8082 + sqlite-vec in overseer.db. Vectors never
            #    leave the host (Slice 13 posture).
            Route("GET",  "/vector/status",
                  self._http_vector_status),
            Route("POST", "/vector/backfill",
                  self._http_vector_backfill),
            Route("POST", "/vector/search",
                  self._http_vector_search),
        ]

    # ── Lifecycle ───────────────────────────────────────────────

    def on_load(self) -> None:
        # Step 1: open OverseerDB FIRST so the overseer schema exists on
        # the plugin's DB before anything else touches it. Same pattern
        # as the pet plugin (proven by 2c2d).
        # Cloud migration P0 (2026-07-20): OVERSEER_DB_PATH env overrides
        # the in-tree default so the cloud container can point at its
        # volume. Unset = plugins/overseer/data/overseer.db, unchanged.
        _env_db = os.environ.get("OVERSEER_DB_PATH", "").strip()
        overseer_db_path = Path(_env_db) if _env_db \
            else self.api.plugin_data / "overseer.db"
        if self.api.db is not None:
            try:
                self.api.db.close()
            except Exception:
                pass
        self.overseer_db = OverseerDB(str(overseer_db_path))
        self.api.db = self.overseer_db
        self.api.log.info("overseer.db opened (schema + helpers ready)")

        # Step 2: real CoreMemoryRO replaces the runtime's _NullCoreMemoryRO.
        # Read-only mode means overseer cannot write to cortex.db even if
        # the code tries. cortex.db stays the user's source of truth.
        self.core_memory = CoreMemoryRO(self.api.core_db_path)
        self.api.core_memory = self.core_memory
        if self.core_memory.is_open:
            self.api.log.info("core_memory wired (read-only on %s)",
                              self.api.core_db_path)
        else:
            self.api.log.warning("core_memory: cortex.db missing or unopenable "
                                 "(reads will return empty)")

        # Step 3: real LLMRouter — three backends with fallback chain.
        # Reads [llm] section from this plugin's own plugin.toml; secrets
        # (OpenRouter API key) come from ~/.cortex/secrets.toml on Pi.
        # Cloud migration P0 fix (2026-07-20): plugin.toml lives with the
        # CODE, not the data dir. plugin_data.parent only worked because
        # data/ used to sit inside the plugin folder; CORTEX_PLUGIN_DATA_DIR
        # relocates data, so resolve from this file's location instead.
        plugin_folder = Path(__file__).resolve().parent
        try:
            with open(plugin_folder / "plugin.toml", "rb") as f:
                manifest = tomllib.load(f)
            llm_cfg = manifest.get("llm", {})
        except Exception as e:
            self.api.log.warning(
                "could not read plugin.toml [llm] (%s); using defaults", e)
            llm_cfg = {}
        self.llm = LLMRouter(manifest_llm=llm_cfg, db=self.overseer_db)
        self.api.llm = self.llm
        self.api.log.info(
            "llm router wired (default backend=%s, fallback=%s)",
            llm_cfg.get("backend", "openrouter"),
            llm_cfg.get("fallback", []),
        )

        # Step 4: seed Session 0 if overseer.db is fresh. Idempotent —
        # the ingester checks overseer_state.session_0_seeded.
        seed_path = self.api.plugin_assets / "session_0_seed.md"
        try:
            self._seed_summary = ingest_seed(self.overseer_db, seed_path)
            if self._seed_summary.get("already_seeded"):
                self.api.log.info("Session 0 already seeded; skipped")
            else:
                self.api.log.info("Session 0 seeded: %s", self._seed_summary)
        except Exception as e:
            # Don't take down the plugin if the seed parse fails — the
            # rest of the overseer can still run, and POST /ingest-session-0
            # exists as a retry path.
            self.api.log.exception("Session 0 ingest failed: %s", e)
            self._seed_summary = {"error": str(e)}

        # Step 5: start the background consolidation loop. Heartbeat-
        # pattern thread; safe to start regardless of seed outcome.
        # Disabled if [config].loop_enabled = false.
        try:
            self.loop = OverseerLoop(
                db=self.overseer_db,
                llm=self.llm,
                core_memory=self.core_memory,
                config=self.api.config,
                log=self.api.log,
            )
            started = self.loop.start()
            if not started:
                self.api.log.info(
                    "loop not started (CORTEX_LOOP_MODE=external or "
                    "loop_enabled=false; see loop log line above)")
        except Exception as e:
            self.api.log.exception("loop init failed: %s", e)

        # Boot-read the overseer journal — the thinking layer of prior
        # instances. Per locked design (3f.5/#1): future instances read
        # the journal at boot BEFORE the structured tables, so the
        # interpretive frame of the predecessor is visible from the
        # start. We just log here; the chat persona pulls them into
        # actual context when it builds prompts.
        try:
            n = int(self.api.config.get("loop_journal_boot_read_n", 5))
            recent_journal = self.overseer_db.recent_journal_entries(
                limit=max(1, n))
            if recent_journal:
                self.api.log.info(
                    "overseer journal: %d entries on file; "
                    "most recent %d shown for boot context:",
                    self.overseer_db.journal_count(), len(recent_journal))
                for e in recent_journal:
                    body = (e.get("body") or "").replace("\n", " ")
                    self.api.log.info(
                        "  [%s prov=%s] %s",
                        (e.get("written_at") or "")[:19],
                        e.get("provisionality"),
                        body[:200] + ("..." if len(body) > 200 else ""),
                    )
            else:
                self.api.log.info(
                    "overseer journal: empty — this instance is "
                    "writing the first entries")
        except Exception as e:
            self.api.log.warning("journal boot-read failed: %s", e)

        snap = self.overseer_db.overseer_snapshot()
        self.api.log.info("overseer ready (slice 3f.5): %s", snap)

    def on_unload(self) -> None:
        if self.loop is not None:
            try:
                self.loop.stop(timeout=5.0)
            except Exception:
                pass
        if self.core_memory is not None:
            try:
                self.core_memory.close()
            except Exception:
                pass
        if self.overseer_db is not None:
            try:
                self.overseer_db.close()
            except Exception:
                pass
        self.api.log.info("plugin overseer unloaded")

    # ── HTTP handlers ───────────────────────────────────────────

    def _http_status(self, payload):
        """GET /plugins/overseer/status — what's in overseer.db right now."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        snap = self.overseer_db.overseer_snapshot()
        core_stats = self.core_memory.get_stats() if self.core_memory else {}
        wm_built_at = self.overseer_db.get_overseer_state(
            "working_memory_built_at")
        last_tick_at = self.overseer_db.get_overseer_state("last_tick_at")
        return {
            "ok": True,
            "plugin": "overseer",
            "version": "0.1.0",
            "overseer_db": snap,
            "core_memory_open": (self.core_memory is not None
                                 and self.core_memory.is_open),
            "core_db_path": str(self.api.core_db_path),
            "core_stats": core_stats,
            "seed_summary": self._seed_summary,
            "llm_default_backend": (self.llm._llm.get("backend")
                                    if self.llm else None),
            "loop_running": (self.loop is not None
                             and self.loop.is_running()),
            "last_tick_at": last_tick_at,
            "working_memory_built_at": wm_built_at,
        }

    def _http_ingest_session_0(self, payload):
        """POST /plugins/overseer/ingest-session-0 — manual seed (re)trigger.

        Body: {"force": true} re-ingests even if the flag is set.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        seed_path = self.api.plugin_assets / "session_0_seed.md"
        force = bool(payload.get("force", False))
        try:
            result = ingest_seed(self.overseer_db, seed_path, force=force)
            self._seed_summary = result
            return {"ok": True, "result": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _http_seed(self, payload):
        """GET /plugins/overseer/seed — show seed metadata + parse summary."""
        return {"ok": True, "summary": self._seed_summary}

    def _http_llm_test(self, payload):
        """POST /plugins/overseer/llm/test — proves the router works.

        Body: {"prompt": "...", "backend"?, "model"?, "system"?,
               "max_tokens"?, "temperature"?, "purpose"?}
        Returns the router's full response dict (text, latency, cost, etc.).
        """
        if self.llm is None:
            return {"ok": False, "error": "llm router not initialized"}
        prompt = payload.get("prompt") or "Reply with one short sentence."
        try:
            result = self.llm.complete(
                prompt,
                backend=payload.get("backend"),
                model=payload.get("model"),
                system=payload.get("system"),
                max_tokens=_as_int(payload, "max_tokens", 256, max_value=4096),
                temperature=float(payload.get("temperature", 0.7)),
                purpose=payload.get("purpose", "test"),
            )
            return {"ok": result.get("ok", False), "result": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _http_llm_calls(self, payload):
        """GET /plugins/overseer/llm/calls?limit=20 — recent llm_calls log."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 20, max_value=200)
        return {"ok": True, "calls": self.overseer_db.recent_llm_calls(limit)}

    def _http_llm_stats(self, payload):
        """GET /plugins/overseer/llm/stats?days=7 — aggregated by backend."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        days = _as_int(payload, "days", 7, max_value=365)
        return {"ok": True, "stats": self.overseer_db.llm_call_stats(days),
                "period_days": days}

    def _http_llm_attribution(self, payload):
        """GET /plugins/overseer/llm/attribution?days=7 — Slice 14.6
        CP1: per-model + per-purpose cost breakdown. Surfaces which
        model did how much work for which task type, at what cost —
        the data needed to decide whether a routing choice is paying
        off (or to spot a routine task that quietly drifted onto an
        expensive model)."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        days = _as_int(payload, "days", 7, max_value=365)
        return {"ok": True, **self.overseer_db.llm_attribution_stats(days)}

    def _http_summarize_recent(self, payload):
        """POST /plugins/overseer/summarize-recent — end-to-end smoke test.

        Pulls the last N notes from cortex.db, asks the LLM for a one-line
        gist, writes the result as a summaries_gist row. Proves the
        CoreMemoryRO + LLMRouter + OverseerDB pipeline is healthy.

        Body: {"limit"?: int (1-50, default 10), "backend"?: str}
        """
        if self.overseer_db is None or self.llm is None or self.core_memory is None:
            return {"ok": False, "error": "overseer not fully initialized"}
        limit = _as_int(payload, "limit", 10, max_value=50)
        notes = self.core_memory.recent_notes(limit=limit)
        if not notes:
            return {"ok": False, "error": "no notes to summarize"}

        # Build a compact prompt — just text content, dated.
        lines = []
        for n in reversed(notes):  # chronological
            ts = (n.get("created_at") or "")[:16]
            content = (n.get("content") or "").strip()
            if content:
                lines.append("- [{}] {}".format(ts, content[:280]))
        body = "\n".join(lines)
        # Slice 3f.5 reframed prompt: gist drops everything but THE CHANGE
        prompt = recent_notes_gist_prompt(body=body)
        # Slice 3f: paired generation when configured.
        use_paired = bool(self.api.config.get(
            "loop_paired_generation", True)) and not payload.get("backend")
        paired = None
        if use_paired:
            paired = paired_generate(
                llm=self.llm, prompt=prompt,
                max_tokens=120, temperature=0.5,
                purpose="summarize-recent",
            )
            result = paired["opus"]
        else:
            result = self.llm.complete(
                prompt,
                backend=payload.get("backend"),
                max_tokens=120,
                temperature=0.5,
                purpose="summarize-recent",
            )

        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "llm failed"),
                    "llm_result": result}

        gist_text = (result.get("text") or "").strip().strip('"').strip()
        gist_id = self.overseer_db.add_gist(
            gist_text,
            period_label="recent-{}-notes".format(len(notes)),
            confidence="med",
            tags=["auto", "summarize-recent"],
        )

        dialectic_id = None
        if paired and paired.get("ok"):
            try:
                dialectic_id = write_dialectic_row(
                    db=self.overseer_db, paired=paired,
                    artifact_type="gist", artifact_id=gist_id,
                    purpose="summarize-recent",
                    source_context="recent {} notes".format(len(notes)),
                )
            except Exception as e:
                self.api.log.warning("dialectic write failed: %s", e)

        return {
            "ok": True,
            "gist_id": gist_id,
            "gist": gist_text,
            "notes_summarized": len(notes),
            "backend": result.get("backend"),
            "model": result.get("model"),
            "latency_ms": result.get("latency_ms"),
            "cost_usd": result.get("cost_usd"),
            "degraded": result.get("degraded"),
            "paired": bool(paired),
            "dialectic_id": dialectic_id,
            "diff": paired.get("diff") if paired else None,
        }

    def _http_themes(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 20, max_value=200)
        rows = self.overseer_db.recent_themes(limit)
        for r in rows:
            r["tags"] = self.overseer_db.get_tags_for("summaries_theme", r["id"])
        return {"ok": True, "themes": rows}

    def _http_episodes(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 20, max_value=200)
        rows = self.overseer_db.recent_episodes(limit)
        for r in rows:
            r["tags"] = self.overseer_db.get_tags_for("summaries_episode", r["id"])
        return {"ok": True, "episodes": rows}

    def _http_questions(self, payload):
        """GET /plugins/overseer/questions
        ?limit=N
        ?include_evidence=1   include recent_evidence on each question
        ?lifecycle=...        optional filter: active | dormant | partially_answered | resolved | abandoned
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 50, max_value=200)
        with_evidence = str(payload.get(
            "include_evidence", "")).lower() in ("1", "true", "yes")
        lifecycle = (payload.get("lifecycle") or "").strip()
        if lifecycle:
            rows = self.overseer_db.all_questions_by_lifecycle(
                lifecycles=[lifecycle], limit=limit)
        else:
            rows = self.overseer_db.active_questions(limit)
        for r in rows:
            r["tags"] = self.overseer_db.get_tags_for(
                "open_questions", r["id"])
        if with_evidence:
            decorated = []
            recent_n = _as_int(payload, "recent_n", 5, max_value=20)
            for r in rows:
                d = self.overseer_db.question_with_evidence(
                    r["id"], recent_n=recent_n)
                if d is not None:
                    decorated.append(d)
            rows = decorated
        return {"ok": True, "questions": rows}

    def _http_question_detail(self, payload):
        """GET /plugins/overseer/questions/get?id=N&recent_n=M

        Full question + recent evidence (with gist bodies decorated)."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        qid = payload.get("id")
        if qid is None:
            return {"ok": False, "error": "missing 'id'"}
        recent_n = _as_int(payload, "recent_n", 20, max_value=200)
        try:
            q = self.overseer_db.question_with_evidence(
                int(qid), recent_n=recent_n)
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be an integer"}
        if not q:
            return {"ok": False, "error": "no such question"}
        return {"ok": True, "question": q}

    def _http_question_lifecycle(self, payload):
        """POST /plugins/overseer/questions/lifecycle

        Body: {"id": N, "lifecycle": "dormant|active|partially_answered|
                                       resolved|abandoned"}
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        qid = payload.get("id")
        lifecycle = (payload.get("lifecycle") or "").strip().lower()
        if qid is None or not lifecycle:
            return {"ok": False, "error": "id and lifecycle required"}
        try:
            ok = self.overseer_db.set_question_lifecycle(
                int(qid), lifecycle)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not ok:
            return {"ok": False, "error": "no such question"}
        return {"ok": True,
                "question": self.overseer_db.get_question(int(qid))}

    def _http_question_upsert(self, payload):
        """POST /plugins/overseer/questions/upsert

        Body (create): {"question": "...", "body": "...",
                        "confidence": "high|med|low", "tags": [...]}
        Body (update): {"id": N, "question": "...", "body": "..."}

        For user-driven question creation/editing. Sets manual flags
        appropriately so auto-classification (if any future rule) won't
        clobber.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        qid = payload.get("id")
        question_text = (payload.get("question") or "").strip()
        body = (payload.get("body") or "").strip()
        confidence = (payload.get("confidence") or "med").strip().lower()
        tags = payload.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        if qid is None and not question_text:
            return {"ok": False,
                    "error": "either 'id' (update) or 'question' (create)"}
        try:
            if qid is not None:
                # Update an existing row
                row = self.overseer_db.get_question(int(qid))
                if not row:
                    return {"ok": False, "error": "no such question"}
                self.overseer_db._conn.execute(
                    "UPDATE open_questions SET question = ?, body = ?, "
                    "confidence = ? WHERE id = ?",
                    (question_text or row["question"],
                     body or row.get("body", ""),
                     confidence or row.get("confidence", "med"),
                     int(qid)),
                )
                self.overseer_db._conn.commit()
                if tags:
                    self.overseer_db.tag_many(
                        "open_questions", int(qid), tags)
                return {"ok": True,
                        "question": self.overseer_db.get_question(int(qid))}
            else:
                new_id = self.overseer_db.add_question(
                    question_text, body=body, confidence=confidence,
                    tags=tags, is_active=True,
                )
                return {"ok": True,
                        "question": self.overseer_db.get_question(new_id)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Slice 3f.5 #4: blindspots + corrections handlers ────────

    def _http_list_blindspots(self, payload):
        """GET /plugins/overseer/blindspots
        ?active_only=1
        ?model=anthropic/claude-opus-4.7    optional filter by what would match
        ?topic=...                           optional filter by what would match
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        active_only = str(payload.get(
            "active_only", "1")).lower() in ("1", "true", "yes")
        model_filter = (payload.get("model") or "").strip()
        topic_filter = (payload.get("topic") or "").strip()
        rows = self.overseer_db.list_blindspots(
            active_only=active_only, limit=200)
        if model_filter or topic_filter:
            from blindspots import applicable_blindspots
            rows = applicable_blindspots(
                db=self.overseer_db,
                model=model_filter, topic=topic_filter,
                record_application=False,
            )
        return {"ok": True, "blindspots": rows,
                "count": len(rows)}

    def _http_upsert_blindspot(self, payload):
        """POST /plugins/overseer/blindspots/upsert

        Body: {"id"?: N, "model_pattern": "*opus*", "topic_pattern": "...",
               "direction": "...", "confidence_adjustment": -1|0|+1,
               "body": "...", "rationale": "...", "confidence": "high|med|low",
               "is_active": true}
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        body = (payload.get("body") or "").strip()
        model_pattern = (payload.get("model_pattern") or "").strip()
        if not body or not model_pattern:
            return {"ok": False,
                    "error": "model_pattern and body required"}
        try:
            bid = self.overseer_db.upsert_blindspot(
                id=payload.get("id"),
                model_pattern=model_pattern,
                body=body,
                topic_pattern=(payload.get("topic_pattern") or ""),
                direction=(payload.get("direction") or "general"),
                confidence_adjustment=int(
                    payload.get("confidence_adjustment") or 0),
                rationale=(payload.get("rationale") or ""),
                confidence=(payload.get("confidence") or "med"),
                source=(payload.get("source") or "user"),
                is_active=bool(payload.get("is_active", True)),
            )
            return {"ok": True,
                    "blindspot": self.overseer_db.get_blindspot(bid)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _http_blindspot_active(self, payload):
        """POST /plugins/overseer/blindspots/active
        Body: {"id": N, "is_active": true|false}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        bid = payload.get("id")
        if bid is None:
            return {"ok": False, "error": "missing 'id'"}
        is_active = bool(payload.get("is_active", True))
        try:
            ok = self.overseer_db.set_blindspot_active(int(bid), is_active)
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be an integer"}
        if not ok:
            return {"ok": False, "error": "no such blindspot"}
        return {"ok": True,
                "blindspot": self.overseer_db.get_blindspot(int(bid))}

    def _http_log_correction(self, payload):
        """POST /plugins/overseer/corrections
        Body: {"what_was_wrong": "...", "user_correction": "...",
               "model": "...", "artifact_table": "...", "artifact_id": N,
               "topic": "...", "severity": "med", "source": "manual"}
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        what = (payload.get("what_was_wrong") or "").strip()
        if not what:
            return {"ok": False, "error": "missing 'what_was_wrong'"}
        try:
            cid = self.overseer_db.log_correction(
                model=(payload.get("model") or ""),
                artifact_table=(payload.get("artifact_table") or ""),
                artifact_id=payload.get("artifact_id"),
                topic=(payload.get("topic") or ""),
                what_was_wrong=what,
                user_correction=(payload.get("user_correction") or ""),
                severity=(payload.get("severity") or "med"),
                source=(payload.get("source") or "manual"),
            )
            return {"ok": True, "correction_id": cid}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _http_list_corrections(self, payload):
        """GET /plugins/overseer/corrections
        ?undistilled_only=1   only those not yet turned into blindspots"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 100, max_value=1000)
        undistilled = str(payload.get(
            "undistilled_only", "")).lower() in ("1", "true", "yes")
        return {
            "ok": True,
            "corrections": self.overseer_db.list_corrections(
                limit=limit, undistilled_only=undistilled),
            "total": self.overseer_db.correction_count(),
            "undistilled": self.overseer_db.correction_count(
                undistilled_only=True),
        }

    # ── Slice 3g checkpoint 2: drill-down detail ───────────────

    # Map detail-token prefix → table name for pull_event recording.
    # Mirrors detail._TABLE_TO_PREFIX inverted but lives here to avoid
    # cross-module import churn.
    _PREFIX_TO_TABLE = {
        "q":    "open_questions",
        "p":    "patterns",
        "d":    "drift_observations",
        "g":    "summaries_gist",
        "e":    "summaries_episode",
        "t":    "summaries_theme",
        "r":    "automation_rollups",
        "n":    "future_overseer_notes",
        "j":    "overseer_journal",
        "b":    "known_blindspots",
        "dial": "dialectic_open",
        "nar":  "temporal_narratives",        # added 2026-05-27
        "hj":   "human_journal_entries",      # added 2026-05-27
    }

    def _http_detail(self, payload):
        """GET /plugins/overseer/detail?token=<prefix>:<id>

        Resolve a working_memory token to its full row + tags +
        type-specific context + suggested next-step tokens. Two depths:
        the working_memory artifact gives you breadth, this gives you
        focused depth on one cell of it.

        Phase 1 (2026-05-27): every successful drill records a pull_event
        so the overseer can see which abstractions consumers keep
        bouncing off of.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        token = str(payload.get("token", "")).strip()
        if not token:
            return {"ok": False, "error": "token query param is required"}
        caller_id = str(payload.get("caller_id") or "").strip() or None
        try:
            result = resolve_detail(self.overseer_db, token)
        except TokenError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("detail resolution failed for %r", token)
            return {"ok": False, "error": "detail failed: " + str(e)}
        # Record a pull_event for successful drills.
        if isinstance(result, dict) and result.get("ok"):
            prefix, _, rest = token.partition(":")
            table = self._PREFIX_TO_TABLE.get(prefix)
            if table and rest.isdigit():
                self.overseer_db.record_pull_event(
                    artifact_table=table,
                    artifact_id=int(rest),
                    surface="mcp:cortex_overseer_detail",
                    caller_id=caller_id,
                )
        return result

    # ── Slice 3h: insight generation ───────────────────────────

    def _http_insight_scan_now(self, payload):
        """POST /plugins/overseer/insight/scan-now
        Body: {"project": "<tag>", "days": 7}
        Manually triggers an insight scan for one project.
        Cost-capped (defaults to insight_scan_max_cost_usd_per_scan).
        """
        if self.overseer_db is None or self.llm is None:
            return {"ok": False, "error": "overseer not fully initialized"}
        project = str(payload.get("project", "")).strip()
        if not project:
            return {"ok": False, "error": "project is required"}
        days = _as_int(payload, "days", int(self.api.config.get(
            "insight_scan_default_days", 7)), max_value=90)
        max_cost = float(self.api.config.get(
            "insight_scan_max_cost_usd_per_scan", 0.05))
        try:
            return scan_project_arcs(
                db=self.overseer_db, llm=self.llm, project=project,
                days=days, max_cost_usd=max_cost,
                budget=None,  # manual trigger; daily cap still applies upstream
                triggered_by="manual",
            )
        except Exception as e:
            log.exception("insight scan-now failed")
            return {"ok": False, "error": "scan failed: " + str(e)}

    def _http_insight_pending(self, payload):
        """GET /plugins/overseer/insight/pending
            ?status=pending|confirmed|rejected|edited
            ?kind=theme|pattern|drift
            ?project=<tag>
            ?limit=200"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        status = str(payload.get("status", "") or "").strip() or None
        kind = str(payload.get("kind", "") or "").strip() or None
        project = str(payload.get("project", "") or "").strip() or None
        limit = _as_int(payload, "limit", 200, max_value=1000)
        rows = self.overseer_db.list_pending_interpretations(
            status=status, kind=kind, project=project, limit=limit,
        )
        # Counts for the UI (for status pills).
        all_pending = self.overseer_db.list_pending_interpretations(
            status="pending", limit=10000)
        all_confirmed = self.overseer_db.list_pending_interpretations(
            status="confirmed", limit=10000)
        all_rejected = self.overseer_db.list_pending_interpretations(
            status="rejected", limit=10000)
        all_edited = self.overseer_db.list_pending_interpretations(
            status="edited", limit=10000)
        return {
            "ok": True,
            "interpretations": rows,
            "counts": {
                "pending": len(all_pending),
                "confirmed": len(all_confirmed),
                "rejected": len(all_rejected),
                "edited": len(all_edited),
            },
        }

    def _http_insight_decide(self, payload):
        """POST /plugins/overseer/insight/decide
        Body: {"id": int, "decision": "confirm"|"reject"|"edit-and-confirm",
               "edit_title"?: str, "edit_body"?: str,
               "review_note"?: str, "reviewed_by"?: str}
        On confirm: creates a real row in patterns/drift/themes.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        interp_id = _as_int(payload, "id", 0)
        if not interp_id:
            return {"ok": False, "error": "id is required"}
        try:
            return apply_pending_interpretation(
                db=self.overseer_db,
                interp_id=interp_id,
                decision=str(payload.get("decision", "")),
                reviewed_by=str(payload.get("reviewed_by", "user")),
                review_note=str(payload.get("review_note", "")),
                edit_title=str(payload.get("edit_title", "")),
                edit_body=str(payload.get("edit_body", "")),
            )
        except Exception as e:
            log.exception("insight decide failed")
            return {"ok": False, "error": "decide failed: " + str(e)}

    def _http_insight_scans(self, payload):
        """GET /plugins/overseer/insight/scans?project=<tag>&limit=20"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        project = payload.get("project")
        if project is not None:
            project = str(project).strip() or None
        limit = _as_int(payload, "limit", 20, max_value=200)
        return {
            "ok": True,
            "scans": self.overseer_db.recent_insight_scans(
                project=project, limit=limit),
        }

    def _http_explorer_graph(self, payload):
        """GET /plugins/overseer/explorer/graph

        Returns the graph data the Hub Explorer renders. Pure
        aggregation — no LLM call. See OverseerDB.explorer_graph for
        the node/edge schema.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            g = self.overseer_db.explorer_graph()
            return {"ok": True, **g}
        except Exception as e:
            log.exception("explorer graph failed")
            return {"ok": False, "error": "graph failed: " + str(e)}

    # ── Slice 4 CP1a: project_summaries routes ─────────────────

    def _http_list_project_summaries(self, payload):
        """GET /plugins/overseer/projects/summary

        List all project_summaries rows. Optional `order_by` payload
        param: last_active_at (default) | session_count |
        cost_usd_estimate | total_minutes | total_messages |
        first_active_at | stats_updated_at | project. Optional
        `descending` (default True).
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        order_by = (payload.get("order_by") or "last_active_at").strip()
        descending = payload.get("descending", True)
        if isinstance(descending, str):
            descending = descending.lower() not in ("0", "false", "no")
        try:
            rows = self.overseer_db.list_project_summaries(
                order_by=order_by, descending=bool(descending),
            )
            # Parse the JSON columns so the client doesn't have to.
            for r in rows:
                r["top_files"] = _safe_json_loads(r.pop("top_files_json", "[]"), [])
                r["models_used"] = _safe_json_loads(r.pop("models_used_json", "{}"), {})
            return {"ok": True, "summaries": rows, "count": len(rows)}
        except Exception as e:
            log.exception("list_project_summaries failed")
            return {"ok": False, "error": "list failed: " + str(e)}

    def _http_get_project_summary(self, payload):
        """GET /plugins/overseer/projects/summary/get?project=<name>

        One project's full summary. 404-ish if no row — caller can
        decide whether to call /refresh first.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        project = (payload.get("project") or "").strip()
        if not project:
            return {"ok": False, "error": "project param required"}
        row = self.overseer_db.get_project_summary(project)
        if not row:
            return {"ok": False, "error": "no summary for project (try refresh)"}
        row["top_files"] = _safe_json_loads(row.pop("top_files_json", "[]"), [])
        row["models_used"] = _safe_json_loads(row.pop("models_used_json", "{}"), {})
        return {"ok": True, "summary": row}

    def _http_refresh_project_summary(self, payload):
        """POST /plugins/overseer/projects/summary/refresh

        Body: {"project": "<name>"}. Recomputes stats from
        imported_sessions + each row's metadata_json (extended stats
        from the backfill). Cheap — no LLM.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        project = (payload.get("project") or "").strip()
        if not project:
            return {"ok": False, "error": "project body required"}
        try:
            return project_summary.refresh_summary(self.overseer_db, project)
        except Exception as e:
            log.exception("refresh_project_summary failed")
            return {"ok": False, "error": "refresh failed: " + str(e)}

    def _http_refresh_all_project_summaries(self, payload):
        """POST /plugins/overseer/projects/summary/refresh-all

        Recomputes every project's summary from scratch. Used by the
        backfill script and by the Hub when the user wants a manual
        rebuild after editing classifications. Cheap (no LLM) but
        scales with imported_sessions row count.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            return project_summary.refresh_all_summaries(self.overseer_db)
        except Exception as e:
            log.exception("refresh_all_project_summaries failed")
            return {"ok": False, "error": "refresh-all failed: " + str(e)}

    # ── Slice 4 CP1b: narrative generation route ───────────────

    def _http_generate_project_narrative(self, payload):
        """POST /plugins/overseer/narrative/generate

        Body: {"project": "<name>", "force": bool (default true),
               "max_cost_usd": float (default
               project_narrative.DEFAULT_MAX_COST_USD_PER_CALL)}

        Manual narrative regen for one project. Bypasses the loop's
        24h/≥3-sessions gate by default — that's the whole point of
        a manual route. Set "force": false to honor the gate (useful
        from a 'refresh stale projects' bulk action if we add one).

        Refreshes deterministic stats first so the narrative reflects
        the latest data. Persists via project_narrative.apply_narrative
        on success.
        """
        if self.overseer_db is None or self.llm is None:
            return {"ok": False, "error": "overseer not fully initialized"}
        project = (payload.get("project") or "").strip()
        if not project:
            return {"ok": False, "error": "project body required"}

        force = payload.get("force", True)
        if isinstance(force, str):
            force = force.lower() not in ("0", "false", "no")
        max_cost = float(payload.get(
            "max_cost_usd",
            project_narrative.DEFAULT_MAX_COST_USD_PER_CALL,
        ))

        # Refresh stats first so we work with current numbers.
        try:
            project_summary.refresh_summary(self.overseer_db, project)
        except Exception as e:
            log.exception("stats refresh failed for %s", project)
            return {"ok": False, "error": "stats refresh failed: " + str(e)}

        row = self.overseer_db.get_project_summary(project)
        if not row:
            return {"ok": False,
                    "error": "no summary for project (no imported sessions?)"}

        if not force:
            should, reason = project_narrative.needs_regen(
                summary_row=row)
            if not should:
                return {"ok": True, "skipped": True, "reason": reason,
                        "project": project}

        # Parse JSON columns for the prompt formatter.
        stats_for_prompt = dict(row)
        stats_for_prompt["top_files"] = _safe_json_loads(
            row.get("top_files_json") or "[]", [])
        stats_for_prompt["models_used"] = _safe_json_loads(
            row.get("models_used_json") or "{}", {})

        try:
            gen = project_narrative.generate_narrative(
                db=self.overseer_db, llm=self.llm,
                project=project, stats=stats_for_prompt,
                max_cost_usd=max_cost,
                triggered_by="manual",
            )
        except Exception as e:
            log.exception("generate_narrative crashed for %s", project)
            return {"ok": False, "error": "generation crashed: " + str(e)}

        if not gen.get("ok"):
            return gen

        try:
            project_narrative.apply_narrative(
                db=self.overseer_db, project=project,
                narrative_text=gen["narrative"],
                cost_usd=gen.get("cost_usd", 0.0),
                session_count_at_update=row.get("session_count", 0),
            )
        except Exception as e:
            log.exception("apply_narrative failed for %s", project)
            return {"ok": False,
                    "error": "narrative generated but persist failed: "
                             + str(e),
                    "narrative_preview": gen["narrative"][:500]}

        return {
            "ok": True,
            "project": project,
            "narrative": gen["narrative"],
            "cost_usd": gen.get("cost_usd", 0.0),
            "model": gen.get("model", ""),
            "latency_ms": gen.get("latency_ms", 0),
        }

    # ── Slice 5: temporal cadence routes ───────────────────────

    def _http_list_temporal(self, payload):
        """GET /plugins/overseer/temporal

        List temporal_narratives rows. Optional `kind` filter:
        daily | weekly | monthly. Newest first.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        kind = (payload.get("kind") or "").strip() or None
        if kind and kind not in ("daily", "weekly", "monthly"):
            return {"ok": False, "error": "kind must be daily/weekly/monthly"}
        limit = _as_int(payload, "limit", 50, max_value=500)
        try:
            rows = self.overseer_db.list_temporal_narratives(
                kind=kind, limit=limit,
            )
            return {"ok": True, "narratives": rows, "count": len(rows)}
        except Exception as e:
            log.exception("list_temporal_narratives failed")
            return {"ok": False, "error": "list failed: " + str(e)}

    def _http_get_temporal(self, payload):
        """GET /plugins/overseer/temporal/get?kind=daily&period_label=2026-05-03"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        kind = (payload.get("kind") or "").strip()
        period_label = (payload.get("period_label") or "").strip()
        if not kind or not period_label:
            return {"ok": False,
                    "error": "kind + period_label required"}
        row = self.overseer_db.get_temporal_narrative(kind, period_label)
        if not row:
            return {"ok": False, "error": "not found"}
        return {"ok": True, "narrative": row}

    def _http_generate_temporal(self, payload):
        """POST /plugins/overseer/temporal/generate

        Body: {"kind": "daily"|"weekly"|"monthly",
               "period_label": optional override (defaults to current
                 period in local TZ),
               "force": bool (default False — when False, returns
                 existing row if one exists for the period)}

        Bypasses the loop's local-time trigger gate. Useful for the
        smoke-test workflow and for the Hub UI's "Generate now"
        button (when CP4 lands).
        """
        if self.overseer_db is None or self.llm is None:
            return {"ok": False, "error": "overseer not fully initialized"}
        kind = (payload.get("kind") or "").strip()
        if kind not in ("daily", "weekly", "monthly", "yearly"):
            return {"ok": False,
                    "error": "kind must be daily/weekly/monthly/yearly"}
        force = payload.get("force", False)
        if isinstance(force, str):
            force = force.lower() in ("1", "true", "yes")

        local_now = temporal_clock.now_local()

        # Period bounds — when caller supplies period_label, derive
        # bounds FROM the label so historical regens actually pull
        # historical data. Slice 14.7.3 (2026-05-26) bug-fix: the
        # earlier code path only replaced the label string while
        # keeping the current-period bounds, which silently
        # regenerated current-period content under fake labels.
        override_label = (payload.get("period_label") or "").strip()
        if override_label:
            try:
                period_start, period_end, period_label = (
                    temporal_clock.bounds_for_label(
                        kind, override_label, local_now))
            except Exception as e:
                return {"ok": False,
                        "error": (f"invalid period_label "
                                  f"'{override_label}' for kind "
                                  f"'{kind}': {e}")}
        else:
            # Default: the same per-kind dispatch the loop uses.
            if kind == "daily":
                period_start, period_end, period_label = (
                    temporal_clock.today_local_bounds(local_now))
            elif kind == "weekly":
                period_start, period_end, period_label = (
                    temporal_clock.week_local_bounds(local_now))
            elif kind == "monthly":
                period_start, period_end, period_label = (
                    temporal_clock.previous_month_local_bounds(
                        local_now))
            else:  # yearly
                period_start, period_end, period_label = (
                    temporal_clock.previous_year_local_bounds(
                        local_now))

        existing = self.overseer_db.get_temporal_narrative(
            kind, period_label)
        if existing and not force:
            return {"ok": True, "skipped": True, "reason": "exists",
                    "narrative": existing}

        gen_fn = {
            "daily":   temporal_narrative.generate_daily,
            "weekly":  temporal_narrative.generate_weekly,
            "monthly": temporal_narrative.generate_monthly,
            "yearly":  temporal_narrative.generate_yearly,
        }[kind]

        try:
            result = gen_fn(
                db=self.overseer_db, llm=self.llm,
                period_start=period_start,
                period_end=period_end,
                period_label=period_label,
                local_now=local_now,
                triggered_by="manual",
            )
        except Exception as e:
            log.exception("temporal generate crashed")
            return {"ok": False, "error": "generate crashed: " + str(e)}

        if not result.get("ok"):
            return result

        # If force=True and existing row, delete first so the
        # UNIQUE(kind, period_label) write succeeds.
        if force and existing:
            self.overseer_db._conn.execute(
                "DELETE FROM temporal_narratives WHERE id = ?",
                (existing["id"],),
            )
            self.overseer_db._safe_commit()

        new_id = temporal_narrative.apply_temporal_narrative(
            db=self.overseer_db, gen_result=result,
            period_start=period_start,
            period_end=period_end,
            period_label=period_label,
            local_created_at=temporal_clock.format_local_iso(local_now),
        )
        if new_id is None:
            return {"ok": False,
                    "error": "narrative generated but persist failed "
                             "(UNIQUE conflict?)",
                    "narrative_preview": result["narrative"][:500]}

        return {
            "ok": True,
            "kind": kind,
            "period_label": period_label,
            "period_start": period_start,
            "period_end": period_end,
            "narrative": result["narrative"],
            "model": result.get("model", ""),
            "cost_usd": result.get("cost_usd", 0),
            "latency_ms": result.get("latency_ms", 0),
            "id": new_id,
        }

    def _http_list_human_journal(self, payload):
        """GET /plugins/overseer/human-journal — newest first."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 100, max_value=500)
        offset = _as_int(payload, "offset", 0)
        rows = self.overseer_db.list_human_journal_entries(
            limit=limit, offset=offset)
        return {"ok": True, "entries": rows, "count": len(rows)}

    def _http_add_human_journal(self, payload):
        """POST /plugins/overseer/human-journal
        Body: {
          "text":             required — entry body,
          "entry_type":       optional — 'free' (default), 'voice',
                              'daily', 'weekly'. 'voice' added
                              2026-06-01 for Google Recorder + Pi
                              wearable transcripts that arrive after
                              the moment of recording.
          "local_created_at": optional — ISO-with-offset override for
                              backdated entries (e.g. a transcript
                              recorded at 09:17 imported later in the
                              day). If absent, defaults to now-local.
                              Must be a parseable ISO string; the
                              backend stores it verbatim into the
                              local_created_at column AND mirrors it
                              into created_at so range queries work.
        }
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        text = (payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text required"}
        entry_type = (payload.get("entry_type") or "free").strip()
        # Expanded 2026-06-01: 'voice' now accepted to cover both Pi
        # wearable voice runtime and Google Recorder imports.
        if entry_type not in ("free", "voice", "daily", "weekly"):
            entry_type = "free"
        # Optional caller override for backdated entries. Defaults to
        # "now" via temporal_clock to preserve the prior behavior.
        local_created_at = str(
            payload.get("local_created_at") or ""
        ).strip()
        if not local_created_at:
            local_created_at = temporal_clock.format_local_iso()
        try:
            new_id = self.overseer_db.add_human_journal_entry(
                text=text,
                entry_type=entry_type,
                local_created_at=local_created_at,
            )
            # If caller passed a backdated local_created_at, mirror it
            # into created_at too so range queries (used by temporal
            # gatherers + vault renderer) place this entry in the
            # right window. Without this, backdated voice entries
            # show up in TODAY's bucket regardless of when they were
            # actually recorded.
            if payload.get("local_created_at"):
                try:
                    # Convert ISO-with-offset to UTC for created_at.
                    import datetime as _dt
                    dt = _dt.datetime.fromisoformat(
                        local_created_at.replace("Z", "+00:00"))
                    utc_iso = dt.astimezone(_dt.timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S")
                    self.overseer_db._conn.execute(
                        "UPDATE human_journal_entries SET created_at "
                        "= ? WHERE id = ?",
                        (utc_iso, new_id),
                    )
                    self.overseer_db._safe_commit()
                except Exception as e:
                    log.warning(
                        "could not normalize created_at for backdated "
                        "human-journal #%s: %s", new_id, e,
                    )
            return {"ok": True, "id": new_id,
                    "entry_type": entry_type,
                    "local_created_at": local_created_at}
        except Exception as e:
            log.exception("add_human_journal failed")
            return {"ok": False, "error": str(e)}

    def _http_delete_human_journal(self, payload):
        """POST /plugins/overseer/human-journal/delete
        Body: {"id": <int>}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        entry_id = payload.get("id")
        if entry_id is None:
            return {"ok": False, "error": "id required"}
        try:
            n = self.overseer_db.delete_human_journal_entry(entry_id)
            return {"ok": True, "deleted": n}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Slice 6: people routes ────────────────────────────────

    def _http_list_people(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 200, max_value=500)
        offset = _as_int(payload, "offset", 0)
        order_by = (payload.get("order_by")
                    or "last_interacted_at").strip()
        rows = self.overseer_db.list_people(
            limit=limit, offset=offset, order_by=order_by)
        for r in rows:
            _parse_people_json(r)
        return {"ok": True, "people": rows, "count": len(rows)}

    def _http_get_person(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        person_id = payload.get("id")
        if person_id is None:
            return {"ok": False, "error": "id required"}
        try:
            row = self.overseer_db.get_person(int(person_id))
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be integer"}
        if not row:
            return {"ok": False, "error": "not found"}
        _parse_people_json(row)
        # Include linked projects
        try:
            row["linked_projects"] = self.overseer_db.projects_for_person(
                row["id"])
        except Exception as e:
            log.warning("projects_for_person failed: %s", e)
        return {"ok": True, "person": row}

    def _http_search_people(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        query = (payload.get("q") or payload.get("query") or "").strip()
        limit = _as_int(payload, "limit", 50, max_value=200)
        rows = self.overseer_db.search_people(query, limit=limit)
        for r in rows:
            _parse_people_json(r)
        return {"ok": True, "people": rows, "count": len(rows),
                "query": query}

    def _http_add_person(self, payload):
        """POST /plugins/overseer/people/add — idempotent on
        case-insensitive name."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        name = (payload.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        try:
            r = self.overseer_db.add_person(
                name=name,
                display_name=payload.get("display_name", "") or "",
                online_handles=_safe_list(
                    payload.get("online_handles")),
                social_links=_safe_list(payload.get("social_links")),
                areas_of_expertise=_safe_list(
                    payload.get("areas_of_expertise")),
                notes=(payload.get("notes") or "").strip(),
                tags=_safe_list(payload.get("tags")),
                aliases=_safe_list(payload.get("aliases")),
                last_interacted_at=payload.get("last_interacted_at"),
                created_by_agent=(payload.get("created_by_agent")
                                  or "manual"),
                created_by_session_id=(
                    payload.get("created_by_session_id") or ""),
            )
        except Exception as e:
            log.exception("add_person failed")
            return {"ok": False, "error": str(e)}
        _parse_people_json(r["person"])
        return {"ok": True, "person": r["person"], "created": r["created"]}

    def _http_update_person(self, payload):
        """POST /plugins/overseer/people/update — partial update.
        Fields not in the body are unchanged. Notes have two modes:
        notes_append (default for agents — preserves history) and
        notes_replace (for manual UI edits)."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        person_id = payload.get("id")
        if person_id is None:
            return {"ok": False, "error": "id required"}
        try:
            updated = self.overseer_db.update_person(
                int(person_id),
                display_name=payload.get("display_name"),
                online_handles=_safe_list_or_none(
                    payload.get("online_handles")),
                social_links=_safe_list_or_none(
                    payload.get("social_links")),
                areas_of_expertise=_safe_list_or_none(
                    payload.get("areas_of_expertise")),
                tags=_safe_list_or_none(payload.get("tags")),
                aliases=_safe_list_or_none(payload.get("aliases")),
                notes_append=payload.get("notes_append"),
                notes_replace=payload.get("notes_replace"),
                last_interacted_at=payload.get("last_interacted_at"),
            )
        except Exception as e:
            log.exception("update_person failed")
            return {"ok": False, "error": str(e)}
        if not updated:
            return {"ok": False, "error": "not found"}
        _parse_people_json(updated)
        return {"ok": True, "person": updated}

    def _http_people_merge(self, payload):
        """POST /plugins/overseer/people/merge

        Body: {"from_id": int, "into_id": int, "dry_run": bool (default
               true), "agent": str (optional)}

        Folds the duplicate `from_id` row into the canonical `into_id`
        row: re-points project_people + phone_contacts references, unions
        the JSON list fields, appends the loser's notes to the survivor,
        and archives the loser via merged_into_id (NEVER deletes it).

        dry_run defaults to TRUE — callers must pass dry_run=false to
        actually mutate. The dry-run return is the exact plan that an
        execute run will carry out.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        from_id = payload.get("from_id")
        into_id = payload.get("into_id")
        if from_id is None or into_id is None:
            return {"ok": False, "error": "from_id + into_id required"}
        dry_run = payload.get("dry_run", True)
        if isinstance(dry_run, str):
            dry_run = dry_run.strip().lower() not in ("0", "false", "no")
        try:
            return self.overseer_db.merge_people(
                int(from_id), int(into_id), dry_run=bool(dry_run),
                agent=(payload.get("agent") or "looper"))
        except Exception as e:
            log.exception("people_merge failed")
            return {"ok": False, "error": str(e)}

    def _http_delete_person(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        person_id = payload.get("id")
        if person_id is None:
            return {"ok": False, "error": "id required"}
        n = self.overseer_db.delete_person(int(person_id))
        return {"ok": True, "deleted": n}

    # ── person_notes routes (2026-06-13 taxonomy build) ──────────────

    def _http_list_person_notes(self, payload):
        """GET /plugins/overseer/people/notes?person_id=N — structured
        notes about a person, newest first, live (non-superseded) only
        unless include_superseded is set."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        person_id = payload.get("person_id") or payload.get("id")
        if person_id is None:
            return {"ok": False, "error": "person_id required"}
        include_superseded = payload.get("include_superseded") in (
            True, "1", "true", "yes")
        try:
            notes = self.overseer_db.list_person_notes(
                int(person_id),
                include_superseded=bool(include_superseded),
                limit=_as_int(payload, "limit", 200, max_value=500))
        except (TypeError, ValueError):
            return {"ok": False, "error": "person_id must be integer"}
        return {"ok": True, "notes": notes, "count": len(notes)}

    def _http_add_person_note(self, payload):
        """POST /plugins/overseer/people/notes/add — append a structured
        note carrying the taxonomy axes. Body: {person_id, body,
        provenance?, modality?, note_kind?, created_by_agent?}."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        person_id = payload.get("person_id") or payload.get("id")
        body = (payload.get("body") or "").strip()
        if person_id is None or not body:
            return {"ok": False, "error": "person_id + body required"}
        try:
            from temporal import format_local_iso as _fmt_local
            local_created_at = _fmt_local()
        except Exception:
            local_created_at = None
        try:
            note = self.overseer_db.add_person_note(
                int(person_id), body=body,
                provenance=(payload.get("provenance") or "overseer"),
                modality=(payload.get("modality") or "statement"),
                note_kind=(payload.get("note_kind") or "context"),
                created_by_agent=(payload.get("created_by_agent")
                                  or "manual"),
                created_by_session_id=(
                    payload.get("created_by_session_id") or ""),
                local_created_at=local_created_at)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("add_person_note failed")
            return {"ok": False, "error": str(e)}
        if note is None:
            return {"ok": False, "error": "person not found"}
        return {"ok": True, "note": note}

    def _http_delete_person_note(self, payload):
        """POST /plugins/overseer/people/notes/delete — remove one note."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        note_id = payload.get("note_id") or payload.get("id")
        if note_id is None:
            return {"ok": False, "error": "note_id required"}
        try:
            n = self.overseer_db.delete_person_note(int(note_id))
        except (TypeError, ValueError):
            return {"ok": False, "error": "note_id must be integer"}
        return {"ok": True, "deleted": n}

    def _http_link_project_person(self, payload):
        """POST /plugins/overseer/people/link-project
        Body: {"project": str, "person_id": int, "role": str (optional),
               "created_by_agent": str (optional)}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        project = (payload.get("project") or "").strip()
        person_id = payload.get("person_id")
        if not project or person_id is None:
            return {"ok": False,
                    "error": "project + person_id required"}
        try:
            link = self.overseer_db.link_project_person(
                project=project, person_id=int(person_id),
                role=(payload.get("role") or "").strip(),
                created_by_agent=(payload.get("created_by_agent")
                                  or "manual"),
            )
            return {"ok": True, "link": link}
        except Exception as e:
            log.exception("link_project_person failed")
            return {"ok": False, "error": str(e)}

    def _http_unlink_project_person(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        project = (payload.get("project") or "").strip()
        person_id = payload.get("person_id")
        if not project or person_id is None:
            return {"ok": False,
                    "error": "project + person_id required"}
        n = self.overseer_db.unlink_project_person(
            project=project, person_id=int(person_id))
        return {"ok": True, "deleted": n}

    def _http_people_for_project(self, payload):
        """GET /plugins/overseer/people/for-project?project=<name>"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        project = (payload.get("project") or "").strip()
        if not project:
            return {"ok": False, "error": "project required"}
        rows = self.overseer_db.people_for_project(project)
        for r in rows:
            _parse_people_json(r)
        return {"ok": True, "project": project,
                "people": rows, "count": len(rows)}

    def _http_people_stats(self, payload):
        """GET /plugins/overseer/people/stats — cross-cutting
        signal-dense stats. See OverseerDB.people_stats for the
        return shape."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            stats = self.overseer_db.people_stats()
            return {"ok": True, **stats}
        except Exception as e:
            log.exception("people_stats failed")
            return {"ok": False, "error": str(e)}

    # ── Slice 9.3: sibling task dispatch endpoints ─────────────────
    # Read the daily cap from plugin.toml here (not in the DB layer)
    # so the DB stays a pure data store and config lives at the edge.
    def _sibling_daily_cap(self) -> int:
        try:
            return int(self.api.config.get(
                "loop_daily_sibling_dispatches", 20))
        except Exception:
            return 20

    def _http_sibling_dispatch(self, payload):
        """POST /plugins/overseer/sibling/dispatch — create a new task.

        Body: {prompt, created_by?, target?, task_type?,
               preferred_model_tier?, cost_budget_usd?, context?}
        Returns {ok, id, used_today, cap} or {ok: false, error}.

        Normally called by the dispatch_sibling chat tool when the
        overseer is mid-turn; can also be POSTed directly (e.g. from
        Tory's CLI to inject a manual task). The created_by field
        defaults to 'overseer' but should be overridden when a human
        is creating it."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        try:
            return self.overseer_db.sibling_dispatch(
                prompt=prompt,
                created_by=(payload.get("created_by") or "overseer"),
                target=(payload.get("target") or "claude-code"),
                task_type=(payload.get("task_type") or "judgment"),
                preferred_model_tier=(
                    payload.get("preferred_model_tier") or "smart"),
                cost_budget_usd=float(
                    payload.get("cost_budget_usd") or 0.50),
                context=payload.get("context"),
                daily_cap=self._sibling_daily_cap(),
            )
        except Exception as e:
            log.exception("sibling_dispatch failed")
            return {"ok": False, "error": str(e)}

    def _http_sibling_pending(self, payload):
        """GET /plugins/overseer/sibling/pending?target=claude-code

        Returns the list of claimable tasks. Siblings filter by their
        own capability target; 'any'-targeted tasks always surface."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        target = (payload.get("target") or "claude-code").strip() or None
        try:
            limit = int(payload.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        try:
            tasks = self.overseer_db.sibling_pending(
                target=target, limit=limit)
            return {"ok": True, "tasks": tasks, "count": len(tasks)}
        except Exception as e:
            log.exception("sibling_pending failed")
            return {"ok": False, "error": str(e)}

    def _http_sibling_claim(self, payload):
        """POST /plugins/overseer/sibling/claim — atomic claim.

        Body: {id, claimed_by}. Refuses race conditions (another
        sibling already claimed it). Returns the full task on success
        so the sibling has everything it needs without a second
        round-trip."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        task_id = payload.get("id")
        claimed_by = (payload.get("claimed_by") or "").strip()
        if not task_id or not claimed_by:
            return {"ok": False,
                    "error": "id and claimed_by are required"}
        try:
            return self.overseer_db.sibling_claim(
                task_id, claimed_by=claimed_by)
        except Exception as e:
            log.exception("sibling_claim failed")
            return {"ok": False, "error": str(e)}

    def _http_sibling_complete(self, payload):
        """POST /plugins/overseer/sibling/complete — submit result.

        Body: {id, result_text, actual_model_used?, result_cost_usd?,
               dispatch_quality_rating?, dispatch_quality_notes?}
        Reciprocal grading is OPTIONAL but encouraged — siblings rating
        the overseer's dispatch quality is the mitigation against the
        overseer-self-rates-results bias the overseer itself flagged."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        task_id = payload.get("id")
        result_text = (payload.get("result_text") or "").strip()
        if not task_id or not result_text:
            return {"ok": False,
                    "error": "id and result_text are required"}
        try:
            return self.overseer_db.sibling_complete(
                task_id,
                result_text=result_text,
                actual_model_used=(payload.get("actual_model_used") or ""),
                result_cost_usd=float(
                    payload.get("result_cost_usd") or 0.0),
                dispatch_quality_rating=payload.get(
                    "dispatch_quality_rating"),
                dispatch_quality_notes=(
                    payload.get("dispatch_quality_notes") or ""),
            )
        except Exception as e:
            log.exception("sibling_complete failed")
            return {"ok": False, "error": str(e)}

    def _http_sibling_reject(self, payload):
        """POST /plugins/overseer/sibling/reject — pass on a task.

        Body: {id, reason}. Different from `complete` with a bad
        result: rejection means the sibling chose not to attempt it
        (out of scope, ambiguous, would exceed cost budget, etc.).
        Reason text shows up in the overseer's next-tick read."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        task_id = payload.get("id")
        reason = (payload.get("reason") or "").strip()
        if not task_id or not reason:
            return {"ok": False, "error": "id and reason are required"}
        try:
            return self.overseer_db.sibling_reject(task_id, reason=reason)
        except Exception as e:
            log.exception("sibling_reject failed")
            return {"ok": False, "error": str(e)}

    def _http_sibling_recent(self, payload):
        """GET /plugins/overseer/sibling/recent

        Returns recently completed/failed/rejected tasks for the
        overseer's tick loop (or the Hub UI) to integrate.
        Pass ?unread=1 to filter to ones the overseer hasn't rated yet."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            limit = int(payload.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        unread = str(payload.get("unread", "")).lower() in ("1", "true", "yes")
        try:
            tasks = self.overseer_db.sibling_recent_completed(
                limit=limit, unread_to_overseer_only=unread)
            return {"ok": True, "tasks": tasks, "count": len(tasks)}
        except Exception as e:
            log.exception("sibling_recent failed")
            return {"ok": False, "error": str(e)}

    def _http_sibling_stats(self, payload):
        """GET /plugins/overseer/sibling/stats — counts + daily budget.

        Used by the chat freshness section to surface the overseer's
        dispatch posture (how many it's used today, how many pending,
        how many awaiting its read)."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            stats = self.overseer_db.sibling_dispatch_stats(
                daily_cap=self._sibling_daily_cap())
            return {"ok": True, **stats}
        except Exception as e:
            log.exception("sibling_stats failed")
            return {"ok": False, "error": str(e)}

    # ── Slice 10.4 (2026-05-20): ecosystem visualizer ───────────

    def _http_ecosystem(self, payload):
        """GET /plugins/overseer/ecosystem — static map of tools,
        tick steps, hooks, B-agents, C-agents for the Hub's
        ecosystem visualizer (Slice 10.4 Phase 1).

        Returns the structure the Hub renders as a React Flow graph.
        Tools + B-agents are read live from the registries; tick
        steps + hook surfaces are hard-coded since they reflect the
        actual loop.py + chat.py + journal.py wiring."""
        try:
            import chat_tools
            import b_agents as _b_agents
        except Exception as e:
            return {"ok": False, "error": f"registry load failed: {e}"}

        # Categorize each chat tool by name prefix. Categories drive
        # color + grouping in the React Flow rendering.
        def _categorize(name: str) -> str:
            if name.startswith("dispatch_b_"):
                return "b_agent_tool"
            if name == "dispatch_sibling":
                return "sibling_dispatch"
            if name == "compress_chat":
                return "chat_mgmt"
            if name == "accept_c_promotion":
                return "c_promotion"
            if name.startswith(("get_", "search_", "list_")):
                return "read"
            if name in ("file_evidence", "propose_project_merge"):
                return "synthesis"
            if name.startswith(("update_", "create_", "delete_",
                                 "redact_", "emit_", "mark_")):
                return "write"
            if name.startswith("scan_"):
                return "scan"
            return "other"

        # Journal-step block list (mirrors journal.py defensive check).
        # If we add tools here that journal mustn't call, also update
        # journal.py — keeping the same list in two places is OK because
        # it's tiny + the block is defense in depth.
        JOURNAL_BLOCKED = {"dispatch_sibling", "compress_chat"}

        tools = []
        for td in chat_tools.TOOL_DEFINITIONS:
            fn = td.get("function") or {}
            name = fn.get("name") or ""
            if not name:
                continue
            callable_from = ["chat"]
            if name not in JOURNAL_BLOCKED:
                callable_from.append("journal_step")
            tools.append({
                "name": name,
                "description": (fn.get("description") or "")[:400],
                "category": _categorize(name),
                "callable_from": callable_from,
            })

        b_agents = []
        for n, spec in _b_agents.B_AGENTS.items():
            b_agents.append({
                "name": n,
                "marker": spec["marker"],
                "model": spec["model"],
                "max_tokens": spec["max_tokens"],
                "description": (spec["description"]
                                if "description" in spec
                                else "")[:200],
            })

        # Hard-coded tick step structure. Matches the actual order in
        # OverseerLoop._run_one_tick_locked as of Slice 10 CP5. When
        # you reorder or rename steps in loop.py, mirror that here so
        # the visualizer doesn't lie.
        tick_steps = [
            {"id": 0, "name": "temporal_cadence",
             "label": "Step 0: Temporal Cadence",
             "fires_when": "22:00 local trigger (daily/weekly/monthly/yearly)",
             "uses_llm": True,
             "description": "BYPASSES daily budget. Time-anchored "
                            "narratives; missing the window is permanent."},
            {"id": 1, "name": "summarize_completed_sessions",
             "label": "Step 1: Summarize Sessions",
             "fires_when": "every tick if sessions ended since high-water",
             "uses_llm": True,
             "description": "One-line gist per session (the CHANGE)."},
            {"id": 2, "name": "auto_classify_projects",
             "label": "Step 1b: Classify Projects",
             "fires_when": "every tick",
             "uses_llm": False,
             "description": "Cheap COUNT/AVG; routes imports by class."},
            {"id": 3, "name": "summarize_imported_sessions",
             "label": "Step 1c: Summarize Imports",
             "fires_when": "every tick if unprocessed imports exist",
             "uses_llm": True,
             "description": "Per-classification routing. Drains backlog."},
            {"id": 4, "name": "git_ingest",
             "label": "Step 1c.5: Git Ingest",
             "fires_when": "every N hours (loop_git_ingest_interval_hours)",
             "uses_llm": False,
             "description": "Periodic GitHub commit ingester."},
            {"id": 5, "name": "generate_missing_rollups",
             "label": "Step 1d: Rollups",
             "fires_when": "every tick if pending rollups",
             "uses_llm": True,
             "description": "Cheap Sonnet rollups of automation imports."},
            {"id": 6, "name": "tag_untagged_notes",
             "label": "Step 2: Tag Notes",
             "fires_when": "every tick if untagged notes",
             "uses_llm": True,
             "description": "Auto-tag user notes."},
            {"id": 7, "name": "build_working_memory",
             "label": "Step 3: Build Working Memory",
             "fires_when": "every tick",
             "uses_llm": False,
             "description": "No LLM — assembles WM snapshot for chat + journal."},
            {"id": 8, "name": "run_notifications",
             "label": "Step 4: Notifications",
             "fires_when": "every tick",
             "uses_llm": False,
             "description": "Deterministic rule evaluation."},
            {"id": 9, "name": "journal_step",
             "label": "Step 5: Journal (TOOL-ENABLED)",
             "fires_when": "every notable tick (cadence-gated)",
             "uses_llm": True,
             "description": "Slice 9.9: first-person reflection WITH "
                            "tool access. Most tools callable here."},
            {"id": 10, "name": "insight_scans",
             "label": "Step 6: Insight Scans",
             "fires_when": "per-project cadence",
             "uses_llm": True,
             "description": "Sonnet scans gist arcs for theme/pattern/drift."},
            {"id": 11, "name": "distill_corrections",
             "label": "Step 7: Distill Corrections",
             "fires_when": "periodic (loop config)",
             "uses_llm": True,
             "description": "Cluster corrections into proposed blindspots."},
            {"id": 12, "name": "project_narrative_refresh",
             "label": "Step 8: Project Narratives",
             "fires_when": "24h cadence + ≥3 new sessions",
             "uses_llm": True,
             "description": "Per-project Sonnet narrative refresh."},
            {"id": 13, "name": "b_agent_gc",
             "label": "Step 10: B-Agent GC",
             "fires_when": "once per local day",
             "uses_llm": False,
             "description": "Slice 10: drops expired b_invocation_transcripts."},
            {"id": 14, "name": "check_c_graduations",
             "label": "Step 11: C-Graduation Check",
             "fires_when": "every tick (rule_key dedup)",
             "uses_llm": False,
             "description": "Slice 10 CP5: scans B stats; emits proposal "
                            "notification if bar met."},
            {"id": 15, "name": "run_scheduled_c_agents",
             "label": "Step 12: Scheduled C Runs",
             "fires_when": "every tick, fires due C agents",
             "uses_llm": True,
             "description": "Slice 10 CP5: dispatches due C agents."},
        ]

        # Hook surfaces — where execution paths begin.
        hooks = [
            {"id": "boot", "label": "Plugin Boot",
             "description": "Service startup. Loads schema, seeds, "
                            "registries; reads recent journal entries "
                            "as boot context."},
            {"id": "chat", "label": "Chat Turn",
             "description": "User message arrives via POST "
                            "/plugins/overseer/chat. Full tool surface "
                            "(35 tools); includes dispatch_sibling and "
                            "compress_chat which are chat-only."},
            {"id": "journal_step", "label": "Journal Step (Slice 9.9)",
             "description": "Tool-enabled reflection inside a tick. All "
                            "tools EXCEPT dispatch_sibling + compress_chat "
                            "are available (defense-in-depth block). Max "
                            "4 tool iterations per tick."},
            {"id": "tick_scheduled", "label": "Scheduled Tick",
             "description": "Background loop at tick_interval_s (default "
                            "900s = 15min). Runs all 16 steps with budget "
                            "gates. Step 0 (temporal) bypasses daily budget."},
            {"id": "bell_action", "label": "Bell Action",
             "description": "Tory clicks a button in the Hub Bell tab. "
                            "Custom actions on a notification thread "
                            "through to notification_responses; overseer "
                            "reads them next journal step."},
        ]

        # C agents (live registry; usually empty until first graduation)
        c_agents = []
        if self.overseer_db is not None:
            try:
                c_agents = self.overseer_db.list_c_agents(limit=50)
            except Exception as e:
                log.warning("list_c_agents in ecosystem failed: %s", e)

        from datetime import datetime, timezone
        return {
            "ok": True,
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "hooks": hooks,
            "tick_steps": tick_steps,
            "tools": tools,
            "b_agents": b_agents,
            "c_agents": c_agents,
            "counts": {
                "hooks": len(hooks),
                "tick_steps": len(tick_steps),
                "tools": len(tools),
                "b_agents": len(b_agents),
                "c_agents": len(c_agents),
            },
        }

    # ── Slice 10.4 Phase 2 (2026-05-20): runs / activity tab ────

    def _http_runs_recent(self, payload):
        """GET /plugins/overseer/runs/recent?hours=24&limit=200&kinds=

        Returns a timeline of recent runs across all overseer
        surfaces (B/C agents, A-tier siblings, chat turns, journal
        steps) normalized to a common shape. Used by the Hub's
        Activity tab to render the left-side timeline list.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        hours = int((payload or {}).get("hours") or 24)
        limit = int((payload or {}).get("limit") or 200)
        kinds_raw = (payload or {}).get("kinds") or ""
        kinds = (set(k.strip() for k in kinds_raw.split(","))
                 if kinds_raw else None)
        try:
            runs = self.overseer_db.list_recent_runs(
                hours=hours, limit=limit, kinds=kinds)
            return {"ok": True, "hours": hours, "count": len(runs),
                    "runs": runs}
        except Exception as e:
            log.exception("runs_recent failed")
            return {"ok": False, "error": str(e)}

    def _http_runs_detail(self, payload):
        """GET /plugins/overseer/runs/detail?kind=X&id=Y — full
        detail for one run including flow graph nodes/edges,
        full prompt, full output. Used by the Activity tab's
        center panel + detail sidebar."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        kind = ((payload or {}).get("kind") or "").strip()
        run_id = (payload or {}).get("id")
        if not kind or run_id is None:
            return {"ok": False, "error": "kind + id required"}
        try:
            return self.overseer_db.get_run_detail(
                kind=kind, run_id=run_id)
        except Exception as e:
            log.exception("runs_detail failed")
            return {"ok": False, "error": str(e)}

    def _http_runs_export(self, payload):
        """GET /plugins/overseer/runs/export?hours=24 — full bundle
        of all runs in the past N hours with snapshots + outputs.
        Frontend triggers a file download from this response."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        hours = int((payload or {}).get("hours") or 24)
        try:
            bundle = self.overseer_db.export_runs_bundle(hours=hours)
            return {"ok": True, **bundle}
        except Exception as e:
            log.exception("runs_export failed")
            return {"ok": False, "error": str(e)}

    def _http_dispatch_export(self, payload):
        """GET /plugins/overseer/dispatch-export?since=<id>&limit=<n>

        Read-only. Returns completed + rated sibling dispatches in the exact
        shape Lemon Squeezer's /ingest/dispatches expects (metadata only —
        no prompt/response text). The Cortex Desktop connector pulls this,
        POSTs to Lemon, and owns the high-water cursor; Lemon is idempotent
        on dispatch_id, so Core stays stateless. `max_id` is returned to make
        the desktop cursor advance trivial. See Swarm Board dispatch-export
        contract (2026-06-13)."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        since_id = _as_int(payload, "since", 0)
        limit = _as_int(payload, "limit", 500, max_value=2000)
        try:
            rows = self.overseer_db.graded_dispatches_for_export(
                since_id=since_id, limit=limit)
        except Exception as e:
            log.exception("dispatch_export failed")
            return {"ok": False, "error": str(e)}
        max_id = since_id
        for r in rows:
            try:
                max_id = max(max_id, int(r["dispatch_id"]))
            except (TypeError, ValueError):
                pass
        return {"ok": True, "dispatches": rows, "count": len(rows),
                "max_id": max_id}

    def _http_runs_rate(self, payload):
        """POST /plugins/overseer/runs/rate
        body: {sibling_task_id, rating, notes?, dataset_candidate?}

        Rate a run that has a sibling_task_id (B/C dispatches and
        A-tier sibling tasks). Threads into the same
        sibling_rate_result code path the chat tool uses, so the
        rating shows up wherever sibling stats are surfaced."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            tid = int((payload or {}).get("sibling_task_id"))
            rating = int((payload or {}).get("rating"))
        except (TypeError, ValueError):
            return {"ok": False,
                    "error": "sibling_task_id + rating (int) required"}
        if rating < 1 or rating > 5:
            return {"ok": False, "error": "rating must be 1..5"}
        notes = (payload or {}).get("notes") or ""
        dc = bool((payload or {}).get("dataset_candidate"))
        try:
            return self.overseer_db.sibling_rate_result(
                tid, rating=rating, notes=notes,
                dataset_candidate=dc)
        except Exception as e:
            log.exception("runs_rate failed")
            return {"ok": False, "error": str(e)}

    # ── Work-org (2026-05-21): targeted import processing ───────

    def _http_imports_tag_machine(self, payload):
        """POST /plugins/overseer/imports/tag-machine
        body: {machine: "work-ClientA", cwd_likes: ["%ClientA%", ...]}

        Stamp metadata_json.machine on every imported_session whose
        cwd matches one of the LIKE patterns. Non-destructive — only
        adds/overwrites the `machine` key inside the existing JSON
        blob. Lets the work-computer cohort be queried as a unit."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        import json as _json
        machine = (payload or {}).get("machine") or ""
        cwd_likes = (payload or {}).get("cwd_likes") or []
        if not machine or not cwd_likes:
            return {"ok": False,
                    "error": "machine + cwd_likes (non-empty) required"}
        conn = self.overseer_db._conn
        where = " OR ".join(["cwd LIKE ?"] * len(cwd_likes))
        rows = conn.execute(
            f"SELECT id, metadata_json FROM imported_sessions "
            f"WHERE ({where})",
            tuple(cwd_likes),
        ).fetchall()
        tagged = 0
        for r in rows:
            try:
                meta = _json.loads(r["metadata_json"] or "{}")
            except Exception:
                meta = {}
            meta["machine"] = machine
            conn.execute(
                "UPDATE imported_sessions SET metadata_json = ? "
                "WHERE id = ?",
                (_json.dumps(meta, ensure_ascii=False), r["id"]),
            )
            tagged += 1
        self.overseer_db._safe_commit()
        return {"ok": True, "machine": machine, "tagged": tagged}

    def _http_imports_process_targeted(self, payload):
        """POST /plugins/overseer/imports/process-targeted
        body: {cwd_likes: [...], source_likes: [...], limit: int,
               max_cost_usd: float}

        Process ONLY imported_sessions matching cwd_likes and/or
        source_likes (provide at least one). Used to drain a specific
        cohort with a hard cost cap, without touching the rest of the
        unprocessed backlog.

        Slice 14.7.2 (2026-05-26): source_likes added for the grok
        backfill drain. Web-archive imports (grok-com, chatgpt,
        twitter) have no cwd so the cwd filter alone couldn't reach
        them.
        """
        if self.loop is None:
            return {"ok": False, "error": "loop not initialized"}
        cwd_likes = (payload or {}).get("cwd_likes") or []
        source_likes = (payload or {}).get("source_likes") or []
        if not cwd_likes and not source_likes:
            return {"ok": False,
                    "error": "cwd_likes or source_likes (non-empty) required"}
        limit = int((payload or {}).get("limit") or 100)
        max_cost = float((payload or {}).get("max_cost_usd") or 4.0)
        try:
            summary = self.loop.process_imports_targeted(
                cwd_likes=cwd_likes, source_likes=source_likes,
                limit=limit, max_cost_usd=max_cost)
            return {"ok": summary.get("ok", True), "summary": summary}
        except Exception as e:
            log.exception("process_imports_targeted failed")
            return {"ok": False, "error": str(e)}

    # ── Slice 13 (2026-05-21): sensitivity tiers ────────────────

    def _http_sensitivity_status(self, payload):
        """GET /plugins/overseer/sensitivity/status — active rules +
        per-tier session counts."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            return {
                "ok": True,
                "rules": self.overseer_db.get_sensitivity_rules(
                    active_only=False),
                "stats": self.overseer_db.sensitivity_stats(),
            }
        except Exception as e:
            log.exception("sensitivity_status failed")
            return {"ok": False, "error": str(e)}

    def _http_sensitivity_backfill(self, payload):
        """POST /plugins/overseer/sensitivity/backfill — apply the
        active rules to existing imported_sessions.

        body: {only_unset: bool (default true)}
        only_unset=true skips rows that already carry a sensitivity
        so user overrides + scanner promotions aren't clobbered."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        only_unset = bool((payload or {}).get("only_unset", True))
        try:
            result = self.overseer_db.backfill_sensitivity(
                only_unset=only_unset)
            return {"ok": True, **result,
                    "stats": self.overseer_db.sensitivity_stats()}
        except Exception as e:
            log.exception("sensitivity_backfill failed")
            return {"ok": False, "error": str(e)}

    # ── Slice 14.7.3 (2026-05-26): category classifier ──────────

    def _http_category_status(self, payload):
        """GET /plugins/overseer/category/status — counts by category +
        a per-source breakdown of the still-unclassified rows."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            return {"ok": True, **self.overseer_db.category_stats()}
        except Exception as e:
            log.exception("category_status failed")
            return {"ok": False, "error": str(e)}

    def _http_category_backfill_rules(self, payload):
        """POST /plugins/overseer/category/backfill-rules — apply the
        deterministic rule classifier (cwd patterns + sensitivity)
        across all imported_sessions.

        body: {only_unset: bool (default true)}
        only_unset=true skips rows that already carry a non-empty
        category, so LLM-classifier results and manual overrides
        aren't clobbered.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        only_unset = bool((payload or {}).get("only_unset", True))
        try:
            result = self.overseer_db.backfill_categories(
                only_unset=only_unset)
            return {"ok": True, **result,
                    "stats": self.overseer_db.category_stats()}
        except Exception as e:
            log.exception("category_backfill_rules failed")
            return {"ok": False, "error": str(e)}

    def _http_category_set(self, payload):
        """POST /plugins/overseer/category/set — manual override.
        body: {imported_id: str, category: 'work'|'cortex'|'personal'|
               'unclassified'}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        imp_id = (payload or {}).get("imported_id") or ""
        cat = (payload or {}).get("category") or ""
        if not imp_id or not cat:
            return {"ok": False,
                    "error": "imported_id + category required"}
        try:
            ok = self.overseer_db.set_session_category(
                imp_id, category=cat, set_by="manual")
            return {"ok": ok, "imported_id": imp_id, "category": cat}
        except Exception as e:
            log.exception("category_set failed")
            return {"ok": False, "error": str(e)}

    def _http_category_classify_batch(self, payload):
        """POST /plugins/overseer/category/classify-batch — Flash
        classifies a batch of unclassified web-AI sessions.

        body: {limit: int (default 200), max_cost_usd: float
               (default 1.0), source: str (filter, optional)}

        For each unclassified session: parse the first user message
        from the .jsonl, ask Flash to classify as work/cortex/
        personal, write the result. Hard cost cap enforced. Returns
        per-category counts + cost.
        """
        if self.overseer_db is None or self.llm is None:
            return {"ok": False,
                    "error": "overseer not fully initialized"}
        limit = int((payload or {}).get("limit") or 200)
        max_cost = float((payload or {}).get("max_cost_usd") or 1.0)
        source = (payload or {}).get("source") or None
        try:
            from category_classifier import run_batch
            result = run_batch(
                db=self.overseer_db, llm=self.llm,
                limit=limit, max_cost_usd=max_cost,
                source=source, log=log,
            )
            return {"ok": True, **result,
                    "stats": self.overseer_db.category_stats()}
        except Exception as e:
            log.exception("category_classify_batch failed")
            return {"ok": False, "error": str(e)}

    # ── Phase 1 (2026-05-27): corpus search + pull events ──────────
    #
    # The discovery surface external AIs were missing. notes_search
    # reads only the `notes` table (0 rows in practice). This walks
    # the interpretive tables (gists, themes, episodes, patterns,
    # drift, future_overseer_notes, overseer_journal, temporal_
    # narratives, open_questions, known_blindspots, human_journal_
    # entries) and returns hits with drill-down tokens so the caller
    # can fetch full rows via /detail.
    #
    # Each hit is logged as a pull_event so the overseer can see
    # what external AIs are looking for and which gist prompts need
    # to evolve.

    # Maps the search target to (table_name, body_columns, token_prefix,
    # kind_label, where_extras). The body_columns is the list of TEXT
    # columns we substring-match against (joined with OR).
    # Column names verified against actual schema 2026-05-27 (L99 fix):
    #   temporal_narratives.narrative (NOT .body)
    #   overseer_journal.body (NOT .entry)
    #   open_questions.question + .body (both — primary text in .question)
    #   summaries_theme adds .title for richer search
    # Earlier draft of this map had .body for narratives + .entry for
    # journal — both silently returned 0 hits because those columns
    # don't exist. The probe missed it because the original
    # checkpoint only searched gists.
    # Source of truth for kind→table mapping lives in corpus.SEARCH_TARGETS
    # (extracted 2026-05-27 so chat_tools.dispatch_tool can call the same
    # search logic from overseer's chat surface). Kept here as a reference
    # only — do NOT duplicate the mapping; update corpus.py instead.
    _SEARCH_TARGETS = corpus.SEARCH_TARGETS

    def _http_search_corpus(self, payload):
        """GET /plugins/overseer/search

        Substring search across the overseer's interpretive corpus.
        Thin HTTP wrapper — the actual search logic lives in
        `corpus.search_corpus()` (extracted 2026-05-27 so the same
        body can be called from chat_tools.dispatch_tool too).

        Params:
          q          (required)  — substring to match (case-insensitive)
          kinds      (optional)  — comma-separated subset of:
                                   gist,theme,episode,pattern,drift,note,
                                   journal,narrative,question,blindspot,
                                   human. Default: all.
          limit_per_kind (default 5)
          limit_total    (default 40)
          days       (optional)  — restrict to artifacts created within
                                   the last N days
          caller_id  (optional)  — recorded with each pull_event so the
                                   overseer can attribute drills
          record_pulls (default true) — log each hit as a pull_event

        Returns {ok, query, kinds_searched, hits, abstractions, gists,
        raw_refs, total, truncated}. Hits carry token, kind,
        artifact_table, artifact_id, snippet (~200 chars around the
        first match), created_at, and extras.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        q = str((payload or {}).get("q") or "").strip()
        if not q:
            return {"ok": False, "error": "q (query string) is required"}

        record_pulls = (payload or {}).get("record_pulls", True)
        if isinstance(record_pulls, str):
            record_pulls = record_pulls.lower() not in (
                "0", "false", "no", "")

        caller_id = str((payload or {}).get("caller_id") or "").strip() \
            or None

        return corpus.search_corpus(
            self.overseer_db,
            q,
            kinds=str((payload or {}).get("kinds") or ""),
            limit_per_kind=_as_int(payload, "limit_per_kind", 5,
                                    max_value=50),
            limit_total=_as_int(payload, "limit_total", 40,
                                 max_value=200),
            days=_as_int(payload, "days", 0, max_value=3650),
            surface="mcp:cortex_search",
            caller_id=caller_id,
            record_pulls=record_pulls,
        )

    def _http_recent_pull_events(self, payload):
        """GET /plugins/overseer/pull-events?limit=50&surface=...&
        artifact_table=...&days=N"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 50, max_value=500)
        surface = str((payload or {}).get("surface") or "").strip() \
            or None
        artifact_table = str(
            (payload or {}).get("artifact_table") or "").strip() or None
        days_raw = (payload or {}).get("days")
        days = int(days_raw) if days_raw else None
        try:
            events = self.overseer_db.recent_pull_events(
                limit=limit, surface=surface,
                artifact_table=artifact_table, days=days,
            )
            return {"ok": True, "events": events,
                    "count": len(events)}
        except Exception as e:
            log.exception("recent_pull_events failed")
            return {"ok": False, "error": str(e)}

    def _http_pull_event_stats(self, payload):
        """GET /plugins/overseer/pull-events/stats?days=7"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        days = _as_int(payload, "days", 7, max_value=365)
        try:
            return {"ok": True, **self.overseer_db.pull_event_stats(
                days=days)}
        except Exception as e:
            log.exception("pull_event_stats failed")
            return {"ok": False, "error": str(e)}

    # ── Phase 1 (2026-05-27): Claude Desktop import scaffold ───────
    #
    # Anthropic Data Export ZIP. Phase 1 ships a dry-run only —
    # parses the ZIP, reports conversation/message counts + sample,
    # writes nothing. Full ingest is a follow-up slice once the parse
    # shape has been validated against a real export.

    def _http_claude_desktop_dry_run(self, payload):
        """POST /plugins/overseer/imports/claude-desktop/dry-run

        Body: {zip_path: "/abs/path/to/export.zip", preview_n: 3}

        Returns parse results without writing anything to the DB.
        Use this to validate the parser against a real export before
        we ship the actual ingest pass.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        zip_path = str((payload or {}).get("zip_path") or "").strip()
        if not zip_path:
            return {"ok": False, "error": "zip_path is required"}
        try:
            from claude_desktop import parse_claude_desktop_export
        except Exception as e:
            log.exception("claude_desktop import failed")
            return {"ok": False,
                    "error": "module import failed: " + str(e)}
        try:
            result = parse_claude_desktop_export(zip_path)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("claude_desktop parse failed")
            return {"ok": False,
                    "error": "parse failed: " + str(e)}
        # Truncate conversations list for transport — full list is
        # only useful when ingesting. Stats stay correct.
        preview_n = _as_int(payload, "preview_n", 3, max_value=50)
        truncated_convos = result.get("conversations", [])[:preview_n]
        return {
            "ok": result.get("ok", True),
            "zip_path": result.get("zip_path"),
            "zip_sha256": result.get("zip_sha256"),
            "totals": result.get("totals"),
            "errors": result.get("errors", []),
            "preview_conversations": truncated_convos,
            "conversations_returned": len(truncated_convos),
            "conversations_parsed_total": result.get(
                "totals", {}).get("conversations", 0),
        }

    # ── Phase 2 (2026-05-27): vault generator scaffold ────────────
    #
    # Renders interpretive tables to markdown under a configured
    # output directory. Scaffold pass = no atomic swap, no hand-edit
    # preservation, no sensitivity gating yet. See
    # plugins/overseer/vault_generator.py for the full scope.

    # ── Sub-agent tier management (2026-05-27) ─────────────────────
    #
    # Tory's directive: B/C agents run as cheap as possible by default,
    # human pulls the upgrade trigger when output is poor. Tier
    # choices persist across restarts (sub_agent_tiers table).
    # dispatch_b_agent reads from this registry every call and records
    # actual_model_used + last_invoked_at on every successful run.

    def _http_list_sub_agents(self, payload):
        """GET /plugins/overseer/sub-agents

        Returns the full sub-agent registry plus the code-side defaults
        from B_AGENTS so the caller can see what's seeded vs what's
        been customized.

        Each row:
          agent_type, agent_name, model_tier, tier_set_at, tier_set_by,
          notes, last_model_used, last_invoked_at, invocation_count,
          default_tier (from B_AGENTS, may differ from model_tier if
          Tory has upgraded), available_tiers (the valid options)
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            from llm_router import SUB_AGENT_TIER_TO_MODEL
            import b_agents as _b_agents
            rows = self.overseer_db.list_sub_agent_tiers()
            # Annotate each row with the code-side default for compare.
            for r in rows:
                if r.get("agent_type") == "b":
                    spec = _b_agents.B_AGENTS.get(r["agent_name"], {})
                    r["default_tier"] = spec.get("default_tier", "flash")
                    r["default_tier_rationale"] = spec.get(
                        "default_tier_rationale", "")
                else:
                    # C-agents: pull from c_agents.model if present
                    r["default_tier"] = "flash"
                    r["default_tier_rationale"] = ""
                r["current_model"] = SUB_AGENT_TIER_TO_MODEL.get(
                    r.get("model_tier", "flash"),
                    SUB_AGENT_TIER_TO_MODEL["flash"])
            return {
                "ok": True,
                "sub_agents": rows,
                "available_tiers": list(SUB_AGENT_TIER_TO_MODEL.keys()),
                "tier_to_model": SUB_AGENT_TIER_TO_MODEL,
            }
        except Exception as e:
            log.exception("list_sub_agents failed")
            return {"ok": False, "error": str(e)}

    def _http_set_sub_agent_tier(self, payload):
        """POST /plugins/overseer/sub-agents/set-tier

        Body: {agent_type: 'b'|'c', agent_name: str,
               tier: 'flash'|'glm'|'sonnet'|'opus', notes?: str}

        Changes the tier for one sub-agent. Persists across restarts.
        Next dispatch picks up the new tier.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        p = payload or {}
        agent_type = str(p.get("agent_type") or "").strip()
        agent_name = str(p.get("agent_name") or "").strip()
        tier = str(p.get("tier") or "").strip()
        notes = str(p.get("notes") or "").strip()
        if agent_type not in ("b", "c"):
            return {"ok": False,
                    "error": "agent_type must be 'b' or 'c'"}
        if not agent_name:
            return {"ok": False, "error": "agent_name is required"}
        if not tier:
            return {"ok": False,
                    "error": "tier is required (flash|glm|sonnet|opus)"}
        try:
            row = self.overseer_db.set_sub_agent_tier(
                agent_type, agent_name, tier,
                set_by="human", notes=notes,
            )
            return {"ok": True, "row": row}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("set_sub_agent_tier failed")
            return {"ok": False, "error": str(e)}

    def _http_sub_agent_performance(self, payload):
        """GET /plugins/overseer/sub-agents/performance
            ?agent_type=b&agent_name=theme_check&last_n=10

        Quality-rating signal for one sub-agent. Reads the last N
        sibling_tasks completed under claimed_by='b-agent:<name>' or
        'c-agent:<name>' and returns rating stats + recent actual
        models used.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        p = payload or {}
        agent_type = str(p.get("agent_type") or "").strip()
        agent_name = str(p.get("agent_name") or "").strip()
        last_n = _as_int(payload, "last_n", 10, max_value=100)
        if agent_type not in ("b", "c"):
            return {"ok": False,
                    "error": "agent_type must be 'b' or 'c'"}
        if not agent_name:
            return {"ok": False, "error": "agent_name is required"}
        try:
            return {
                "ok": True,
                "agent_type": agent_type,
                "agent_name": agent_name,
                **self.overseer_db.sub_agent_performance(
                    agent_type, agent_name, last_n=last_n),
            }
        except Exception as e:
            log.exception("sub_agent_performance failed")
            return {"ok": False, "error": str(e)}

    # ── Looper log routes (2026-06-05) ─────────────────────────────
    #
    # Used by the /loop Claude Code session to journal each iteration
    # so the next iteration can read what was done. NOT for overseer's
    # own reflection — that's overseer_journal.

    def _http_looper_start(self, payload):
        """POST /plugins/overseer/looper/start

        Body: {mode?: str, session_id?: str, model?: str,
               local_started_at?: str}

        Returns the new looper_log row id + iteration_number. Call
        this at the START of every /loop iteration; finish it via
        /looper/finish at the end.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        p = payload or {}
        try:
            result = self.overseer_db.start_looper_iteration(
                mode=str(p.get("mode") or "general"),
                session_id=str(p.get("session_id") or ""),
                model=str(p.get("model") or ""),
                local_started_at=str(p.get("local_started_at") or ""),
            )
            return {"ok": True, **result}
        except Exception as e:
            log.exception("looper_start failed")
            return {"ok": False, "error": str(e)}

    def _http_looper_finish(self, payload):
        """POST /plugins/overseer/looper/finish

        Body: {
          id: required — the looper_log row id from /looper/start
          summary: str — 1-paragraph TLDR for next iteration
          work_done: list of {category, item, status}
          followups: list of strings — what next iter should consider
          files_changed: list of repo paths touched
          llm_calls_estimate: int — rough total LLM calls this iter
          cost_usd_estimate: float — rough total $ spent
          escalations: list of strings — items requiring Tory's call
          local_ended_at: ISO local with offset
        }
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        p = payload or {}
        loop_id = p.get("id")
        if loop_id is None:
            return {"ok": False, "error": "id is required"}
        try:
            return self.overseer_db.finish_looper_iteration(
                id=int(loop_id),
                summary=str(p.get("summary") or ""),
                work_done=p.get("work_done") or [],
                followups=p.get("followups") or [],
                files_changed=p.get("files_changed") or [],
                llm_calls_estimate=int(
                    p.get("llm_calls_estimate") or 0),
                cost_usd_estimate=float(
                    p.get("cost_usd_estimate") or 0.0),
                escalations=p.get("escalations") or [],
                local_ended_at=str(p.get("local_ended_at") or ""),
            )
        except Exception as e:
            log.exception("looper_finish failed")
            return {"ok": False, "error": str(e)}

    def _http_looper_recent(self, payload):
        """GET /plugins/overseer/looper/recent?limit=10

        Returns recent looper_log entries (most recent first). The
        /loop iteration calls this at boot to read what prior
        iterations did + what they queued for follow-up.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 10, max_value=100)
        try:
            entries = self.overseer_db.recent_looper_entries(
                limit=limit)
            return {"ok": True, "entries": entries,
                    "count": len(entries)}
        except Exception as e:
            log.exception("looper_recent failed")
            return {"ok": False, "error": str(e)}

    # ── Context brief (2026-06-08) ──────────────────────────────
    #
    # Tory's framing 2026-06-08: working_memory currently dumps 29
    # keys, ~half operational (queue depths, sibling stats, gist
    # source distribution, etc.). The first 30s of a new AI session
    # should explain WHO TORY IS + WHAT HE'S WORKING ON + WHAT HE
    # CARES ABOUT — not how many times the overseer ticked last week.
    #
    # /intro is the new surface for that. Leads with Tory-state from
    # USER.md + structured tables; demotes overseer's own operational
    # chatter to a single `ops` sub-key. working_memory stays as
    # overseer's internal state surface (other consumers depend on
    # the existing shape).

    def _read_user_md_brief(self):
        """Pull the human-facing brief from memory/core/USER.md.

        Returns {role, location, neurotype, working_style,
                 sensitive_topics} or {} if the file isn't readable.
        USER.md is gitignored — exists locally on the Pi but not in
        the public repo. If the file moves or its sections change,
        the brief degrades gracefully.
        """
        from pathlib import Path
        candidates = [
            Path("/home/turfptax/cortex-core/memory/core/USER.md"),
            Path(__file__).resolve().parent.parent.parent
                / "memory" / "core" / "USER.md",
        ]
        text = None
        for p in candidates:
            if p.exists():
                try:
                    text = p.read_text(encoding="utf-8")
                    break
                except Exception:
                    continue
        if not text:
            return {}
        # Cheap section-grep: pull out short headlines we know are
        # there. Falls back gracefully if a section is missing.
        import re

        def first_line_after(header_pattern):
            m = re.search(
                rf"^#+\s*{header_pattern}.*?\n(.+?)(?=\n#+\s)",
                text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
            )
            return m.group(1).strip() if m else ""

        def grab_field(label):
            m = re.search(
                rf"\*\*{re.escape(label)}\*\*:?\s*([^\n]+)",
                text, re.IGNORECASE,
            )
            return m.group(1).strip() if m else ""

        brief = {}
        for label, key in (
            ("Full name", "full_name"),
            ("Location", "location"),
            ("Neurotype", "neurotype"),
            ("Title", "title"),
            ("Employer", "employer"),
        ):
            v = grab_field(label)
            if v:
                brief[key] = v
        # Working-style + calibration block: pull bullets under
        # "How to work with Tory" or "Working style".
        def _clean_bullet(s):
            # Strip the leading list-marker once (`- ` or `* `).
            # Don't keep stripping — that would eat markdown **bold**
            # markers that legitimately follow the list bullet.
            s = s.strip()
            for marker in ("- ", "* "):
                if s.startswith(marker):
                    s = s[len(marker):].lstrip()
                    break
            return s

        for header in ("How to work with Tory",
                       "Working style", "Calibration notes for AIs"):
            chunk = first_line_after(re.escape(header))
            if chunk:
                # Trim to first ~10 lines so we don't dump prose.
                lines = [_clean_bullet(l) for l in chunk.splitlines()
                          if l.strip().startswith("-")][:10]
                # Drop empty + truncate each to a reasonable display
                # length so the JSON stays scannable.
                lines = [l[:280] for l in lines if l]
                if lines:
                    brief["working_style"] = lines
                    break
        # Sensitive topics: same pattern, look for the explicit
        # bullets under "Sensitive topics".
        chunk = first_line_after(re.escape("Sensitive topics"))
        if chunk:
            lines = [_clean_bullet(l) for l in chunk.splitlines()
                      if l.strip().startswith("-")][:8]
            lines = [l[:280] for l in lines if l]
            if lines:
                brief["sensitive_topics"] = lines
        return brief

    # ── Vector index handlers (2026-06-10) ──────────────────────

    def _http_vector_status(self, payload):
        """GET /plugins/overseer/vector/status"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        return {"ok": True, **self.overseer_db.vector_status()}

    def _http_vector_backfill(self, payload):
        """POST /plugins/overseer/vector/backfill

        {batches?: int=10, batch_size?: int=32, reembed?: bool}

        Embeds gists that have no vector yet, oldest first. Idempotent:
        call repeatedly until remaining == 0. reembed=true drops every
        vector + the model pin first (the model-swap path)."""
        db = self.overseer_db
        if db is None:
            return {"ok": False, "error": "overseer not initialized"}
        if not db.vec_available:
            return {"ok": False, "error": "sqlite-vec unavailable"}
        if payload.get("reembed"):
            db.drop_all_embeddings()
        batches = _as_int(payload, "batches", 10, max_value=200)
        batch_size = _as_int(payload, "batch_size", 32, max_value=128)
        embedded = 0
        for _ in range(batches):
            ids = db.unembedded_gist_ids(limit=batch_size)
            if not ids:
                break
            n = db.embed_gists(ids)
            if n == 0:
                status = db.vector_status()
                return {"ok": False,
                        "error": "embedding service unavailable "
                                 "mid-backfill",
                        "embedded_this_call": embedded, **status}
            embedded += n
        status = db.vector_status()
        return {"ok": True, "embedded_this_call": embedded,
                "remaining": status["total_gists"] - status["embedded"],
                **status}

    def _http_vector_search(self, payload):
        """POST /plugins/overseer/vector/search

        {q: str, k?: int=10}

        Meaning-search over the gist corpus. Returns gists ranked by
        cosine similarity with g: drill tokens, so callers can chain
        into the existing detail surface."""
        db = self.overseer_db
        if db is None:
            return {"ok": False, "error": "overseer not initialized"}
        if not db.vec_available:
            return {"ok": False, "error": "sqlite-vec unavailable"}
        q = (payload.get("q") or payload.get("query") or "").strip()
        if not q:
            return {"ok": False, "error": "missing q"}
        k = _as_int(payload, "k", 10, max_value=50)
        from embeddings import embed_one
        vec = embed_one(q)
        if vec is None:
            return {"ok": False, "error": "embedding service unavailable"}
        t0 = time.time()
        hits = db.semantic_neighbors(vec, k=k)
        knn_ms = round((time.time() - t0) * 1000, 1)
        results = []
        for h in hits:
            row = db._conn.execute(
                "SELECT id, body, period_label, confidence, created_at "
                "FROM summaries_gist WHERE id = ?",
                (h["gist_id"],)).fetchone()
            if row is None:
                continue
            results.append({
                "token": "g:{}".format(row["id"]),
                "gist_id": row["id"],
                "similarity": round(1.0 - h["distance"], 4),
                "period_label": row["period_label"],
                "confidence": row["confidence"],
                "created_at": row["created_at"],
                "snippet": (row["body"] or "")[:280],
            })
        return {"ok": True, "q": q, "count": len(results),
                "knn_ms": knn_ms, "results": results}

    # ── Day-in-Cortex handlers (2026-07-12) ──────────────────────
    # Permanent memory: everything the corpus holds about one local
    # day, for any date across all years. Backs the Hub's Simples
    # Day panel and the Year heat. Read-only aggregation; the Hub is
    # the user's own private surface, so nothing is sensitivity-
    # filtered here (the gateway connector path has its own gate).

    # Local-day expression: prefer the structural local-with-offset
    # twin column, fall back to the raw timestamp's date prefix.
    @staticmethod
    def _local_day_expr(col):
        return ("substr(COALESCE(NULLIF(local_{c},''), {c}), 1, 10)"
                .format(c=col))

    def _http_day_detail(self, payload):
        """GET /plugins/overseer/day?date=YYYY-MM-DD

        One local day across the corpus: AI sessions (with their gist
        one-liners), logged time entries, health metrics (steps,
        sleep, scores), human journal entries, and the daily
        narrative if one was generated."""
        import re as _re
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        raw = payload.get("date")
        if isinstance(raw, list):  # repeated query param arrives as a list
            raw = raw[-1] if raw else ""
        date = (raw or "").strip() if isinstance(raw, str) else ""
        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return {"ok": False, "error": "date must be YYYY-MM-DD"}
        conn = self.overseer_db._conn
        out = {"ok": True, "date": date}

        day_started = self._local_day_expr("started_at")
        try:
            rows = conn.execute(
                "SELECT id, source, project, started_at, "
                "       local_started_at, duration_minutes, "
                "       message_count, tool_use_count, sensitivity, "
                "       redacted_at "
                "FROM imported_sessions "
                "WHERE {} = ? "
                "ORDER BY COALESCE(NULLIF(local_started_at,''), started_at)"
                .format(day_started), (date,)).fetchall()
            sessions = [dict(r) for r in rows]
            ids = [s["id"] for s in sessions]
            gists = {}
            if ids:
                # Canonical session-to-gist link is
                # processed_imported_sessions.imported_id -> gist_id
                # (raw_pointers is the jsonl-file provenance channel,
                # not the session link).
                marks = ",".join("?" * len(ids))
                for g in conn.execute(
                        "SELECT p.imported_id AS sid, g.body "
                        "FROM processed_imported_sessions p "
                        "JOIN summaries_gist g ON g.id = p.gist_id "
                        "WHERE p.imported_id IN ({}) "
                        "ORDER BY g.id".format(marks),
                        ids):
                    gists[g["sid"]] = g["body"]  # newest gist id wins
            for s in sessions:
                s["gist"] = (gists.get(s["id"]) or "")[:400]
                s["redacted"] = bool(s.pop("redacted_at", None))
            out["sessions"] = sessions
            # Same 16h/session clamp as /day/heat so the Year tooltip
            # and this header can never disagree about a day.
            out["session_minutes"] = sum(
                min(s["duration_minutes"] or 0, 960) for s in sessions)
        except Exception as e:
            log.warning("day: sessions failed: %s", e)
            out["sessions"] = []
            out["session_minutes"] = 0

        try:
            cm = getattr(self.core_memory, "_conn", None)
            if cm is not None:
                rows = cm.execute(
                    "SELECT project_tag, activity_type, description, "
                    "       started_at, local_started_at, duration_minutes "
                    "FROM time_entries WHERE {} = ? "
                    "ORDER BY COALESCE(NULLIF(local_started_at,''), started_at)"
                    .format(day_started), (date,)).fetchall()
                out["time_entries"] = [dict(r) for r in rows]
            else:
                out["time_entries"] = []
        except Exception as e:
            log.warning("day: time_entries failed: %s", e)
            out["time_entries"] = []
        out["logged_minutes"] = sum(
            t.get("duration_minutes") or 0 for t in out["time_entries"])

        try:
            out["health"] = {
                r["metric"]: r["value"] for r in conn.execute(
                    "SELECT metric, MAX(value) AS value FROM health_daily "
                    "WHERE day = ? GROUP BY metric", (date,))}
        except Exception:
            out["health"] = {}  # table absent on installs without the backfill

        try:
            day_created = self._local_day_expr("created_at")
            rows = conn.execute(
                "SELECT text, entry_type, created_at, local_created_at "
                "FROM human_journal_entries WHERE {} = ? "
                "ORDER BY created_at".format(day_created), (date,)).fetchall()
            out["journal"] = [dict(r) for r in rows]
        except Exception as e:
            log.warning("day: journal failed: %s", e)
            out["journal"] = []

        try:
            row = conn.execute(
                "SELECT narrative FROM temporal_narratives "
                "WHERE kind = 'daily' AND period_label = ? "
                "ORDER BY id DESC LIMIT 1", (date,)).fetchone()
            out["narrative"] = row["narrative"] if row else ""
        except Exception:
            out["narrative"] = ""
        return out

    def _http_day_heat(self, payload):
        """GET /plugins/overseer/day/heat?year=YYYY

        Per-day aggregates for a whole year, keyed by local day:
        s = AI-session minutes, sc = session count, t = logged
        time-entry minutes, z = hours slept, p = steps, a =
        active-zone minutes. Feeds the Hub's Year view for any year
        the corpus covers."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            year = int(payload.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        if not (1990 <= year <= 2100):
            return {"ok": False, "error": "year must be 1990-2100"}
        conn = self.overseer_db._conn
        like = "{}-%".format(year)
        days = {}

        def bucket(d):
            return days.setdefault(d, {})

        day_started = self._local_day_expr("started_at")
        try:
            # Per-session contribution clamped to 16h: a session row
            # whose timestamps straddle a long gap must not turn one
            # day into "250h of AI work" in the texture.
            for r in conn.execute(
                    "SELECT {expr} AS d, "
                    "       SUM(MIN(duration_minutes, 960)) AS m, "
                    "       COUNT(*) AS c "
                    "FROM imported_sessions WHERE {expr} LIKE ? "
                    "GROUP BY d".format(expr=day_started), (like,)):
                b = bucket(r["d"])
                b["s"] = r["m"] or 0
                b["sc"] = r["c"] or 0
        except Exception as e:
            log.warning("day/heat: sessions failed: %s", e)

        try:
            cm = getattr(self.core_memory, "_conn", None)
            if cm is not None:
                for r in cm.execute(
                        "SELECT {expr} AS d, SUM(duration_minutes) AS m "
                        "FROM time_entries WHERE {expr} LIKE ? "
                        "GROUP BY d".format(expr=day_started), (like,)):
                    bucket(r["d"])["t"] = r["m"] or 0
        except Exception as e:
            log.warning("day/heat: time_entries failed: %s", e)

        try:
            for r in conn.execute(
                    "SELECT day, metric, MAX(value) AS value "
                    "FROM health_daily WHERE day LIKE ? "
                    "  AND metric IN ('sleep_minutes', 'steps', "
                    "                 'azm_minutes') "
                    "GROUP BY day, metric", (like,)):
                b = bucket(r["day"])
                if r["metric"] == "sleep_minutes":
                    b["z"] = round((r["value"] or 0) / 60.0, 1)
                elif r["metric"] == "azm_minutes":
                    b["a"] = int(r["value"] or 0)
                else:
                    b["p"] = int(r["value"] or 0)
        except Exception:
            pass  # health_daily absent on installs without the backfill
        return {"ok": True, "year": year, "days": days}

    # ── Tech skills + rules handlers (2026-07-12) ────────────────

    def _http_skills_list(self, payload):
        """GET /plugins/overseer/skills, the portfolio index."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            limit = max(1, min(int(payload.get("limit") or 100), 500))
        except (TypeError, ValueError):
            limit = 100
        return {"ok": True,
                "skills": self.overseer_db.list_skills(limit=limit)}

    def _http_skills_get(self, payload):
        """GET /plugins/overseer/skills/get?name=... full entry."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        name = (payload.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        skill = self.overseer_db.get_skill(name)
        if not skill:
            return {"ok": False, "error": f"no skill named '{name}'"}
        return {"ok": True, "skill": skill}

    def _http_skills_log(self, payload):
        """POST /plugins/overseer/skills/log: append a portfolio
        entry (lesson/win/project/tooling/note); creates the skill
        header on first mention. {skill, content, kind?, project?,
        source?, proficiency?, summary?, tools?}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        skill = (payload.get("skill") or "").strip()
        content = (payload.get("content") or "").strip()
        if not skill or not content:
            return {"ok": False, "error": "skill + content required"}
        try:
            r = self.overseer_db.log_skill_entry(
                skill=skill,
                kind=(payload.get("kind") or "note").strip(),
                content=content,
                project=(payload.get("project") or "").strip(),
                source=(payload.get("source") or "").strip(),
                proficiency=payload.get("proficiency"),
            )
            # summary/tools ride the same call when provided.
            if payload.get("summary") is not None or payload.get("tools") is not None:
                self.overseer_db.upsert_skill(
                    name=skill,
                    summary=payload.get("summary"),
                    tools=payload.get("tools"))
                r["skill"] = self.overseer_db.get_skill(skill, log_limit=0)
        except Exception as e:
            log.exception("skills/log failed")
            return {"ok": False, "error": str(e)}
        return {"ok": True, **r}

    def _http_rules_list(self, payload):
        """GET /plugins/overseer/rules?status=&stack=, the decisions log."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            limit = max(1, min(int(payload.get("limit") or 200), 500))
        except (TypeError, ValueError):
            limit = 200
        rules = self.overseer_db.list_rules(
            status=(payload.get("status") or "active").strip(),
            stack=(payload.get("stack") or "").strip() or None,
            limit=limit)
        return {"ok": True, "rules": rules}

    def _http_rules_add(self, payload):
        """POST /plugins/overseer/rules/add: upsert on title.
        {title, rule, stack?, situation?, went_wrong?, what_changed?,
        rationale?, status?, source?}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        title = (payload.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "title required"}
        try:
            r = self.overseer_db.add_rule(
                title=title,
                rule=(payload.get("rule") or "").strip(),
                stack=(payload.get("stack") or "").strip(),
                situation=(payload.get("situation") or "").strip(),
                went_wrong=(payload.get("went_wrong") or "").strip(),
                what_changed=(payload.get("what_changed") or "").strip(),
                rationale=(payload.get("rationale") or "").strip(),
                status=(payload.get("status") or "").strip() or None,
                source=(payload.get("source") or "").strip(),
            )
        except Exception as e:
            log.exception("rules/add failed")
            return {"ok": False, "error": str(e)}
        return {"ok": True, **r}

    def _http_intro(self, payload):
        """GET /plugins/overseer/intro

        Curated context brief for external AIs. Goal per Tory's
        2026-06-08 framing: the first 30 seconds tells you who Tory
        is, what he's working on, what he's thinking about, and what
        matters to him.

        Sections:
          who_is_tory          — pulled from USER.md if readable
          working_on           — top 5 projects by last_active_at
          thinking_about       — top open_questions (high/med, by
                                 evidence_count desc)
          recent_decisions     — most-recent corpus_decisions rows
          recent_themes        — high+med confidence themes
          key_drift            — recent drift observations
          blindspots           — active calibration notes for the AI
                                 reading this
          recent_future_notes  — institutional memory from prior
                                 overseer/looper instances
          ops                  — single demoted sub-key with the
                                 operational counters
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            db = self.overseer_db
            conn = db._conn
            import datetime as _ddt
            brief = {
                "generated_at": _ddt.datetime.now().astimezone()
                                    .isoformat(timespec="seconds"),
            }
            # WHO: USER.md brief
            brief["who_is_tory"] = self._read_user_md_brief()

            # WORKING_ON: top projects by activity
            try:
                rows = db.list_project_summaries(
                    order_by="last_active_at", descending=True,
                )[:5]
                brief["working_on"] = [
                    {
                        "project": r.get("project") or r.get("tag"),
                        "status": r.get("status") or "active",
                        "last_active": (r.get("last_active_at")
                                          or "")[:10],
                        "session_count": r.get("session_count") or 0,
                        "minutes_total": r.get(
                            "active_minutes_total") or 0,
                        "narrative_excerpt": (
                            (r.get("narrative_text") or "")[:280]
                        ),
                    }
                    for r in rows
                ]
            except Exception as e:
                log.warning("intro: projects failed: %s", e)
                brief["working_on"] = []

            # THINKING_ABOUT: high-confidence open questions
            try:
                qrows = conn.execute(
                    "SELECT id, question, body, confidence, "
                    "       lifecycle, evidence_count, "
                    "       last_evidence_at "
                    "FROM open_questions "
                    "WHERE is_active = 1 "
                    "ORDER BY "
                    "  CASE confidence WHEN 'high' THEN 0 "
                    "                  WHEN 'med' THEN 1 ELSE 2 END, "
                    "  evidence_count DESC "
                    "LIMIT 8"
                ).fetchall()
                brief["thinking_about"] = [
                    {
                        "question": q["question"],
                        "confidence": q["confidence"],
                        "lifecycle": q["lifecycle"],
                        "evidence_count": q["evidence_count"],
                        "last_evidence_at": (q["last_evidence_at"]
                                              or "")[:10],
                        "token": f"q:{q['id']}",
                    }
                    for q in qrows
                ]
            except Exception as e:
                log.warning("intro: questions failed: %s", e)
                brief["thinking_about"] = []

            # RECENT DECISIONS: the looper's cycle-2 corpus_decisions
            # mine. Falls back to empty list if the table doesn't
            # exist (older installs).
            try:
                drows = conn.execute(
                    "SELECT id, project, decision_text, decided_on, "
                    "       confidence, people, themes, gist_id "
                    "FROM corpus_decisions "
                    "WHERE decided_on IS NOT NULL "
                    "ORDER BY decided_on DESC, id DESC "
                    "LIMIT 12"
                ).fetchall()
                brief["recent_decisions"] = [
                    {
                        "decision": d["decision_text"],
                        "project": d["project"] or "",
                        "decided_on": (d["decided_on"] or "")[:10],
                        "confidence": d["confidence"],
                        "people": d["people"] or "",
                        "themes": d["themes"] or "",
                        "drill_token": (f"g:{d['gist_id']}"
                                          if d["gist_id"] else None),
                    }
                    for d in drows
                ]
            except Exception as e:
                log.warning("intro: decisions failed: %s", e)
                brief["recent_decisions"] = []

            # RECENT THEMES: high + med confidence only
            try:
                trows = conn.execute(
                    "SELECT id, title, body, confidence "
                    "FROM summaries_theme "
                    "WHERE confidence IN ('high', 'med') "
                    "ORDER BY last_reinforced_at DESC LIMIT 8"
                ).fetchall()
                brief["recent_themes"] = [
                    {
                        "title": t["title"],
                        "confidence": t["confidence"],
                        "claim": (t["body"] or "")[:280],
                        "token": f"t:{t['id']}",
                    }
                    for t in trows
                ]
            except Exception as e:
                log.warning("intro: themes failed: %s", e)
                brief["recent_themes"] = []

            # KEY DRIFT: recent direction changes
            try:
                drrows = conn.execute(
                    "SELECT id, body, direction, confidence, "
                    "       observed_at FROM drift_observations "
                    "WHERE confidence IN ('high', 'med') "
                    "ORDER BY observed_at DESC LIMIT 6"
                ).fetchall()
                brief["key_drift"] = [
                    {
                        "observation": (d["body"] or "")[:280],
                        "direction": d["direction"] or "",
                        "confidence": d["confidence"],
                        "observed_at": (d["observed_at"]
                                          or "")[:10],
                        "token": f"d:{d['id']}",
                    }
                    for d in drrows
                ]
            except Exception as e:
                log.warning("intro: drift failed: %s", e)
                brief["key_drift"] = []

            # BLINDSPOTS: calibration notes the AI reading should know
            try:
                brows = conn.execute(
                    "SELECT id, body, rationale, confidence "
                    "FROM known_blindspots "
                    "WHERE is_active = 1 "
                    "ORDER BY apply_count DESC LIMIT 6"
                ).fetchall()
                brief["blindspots"] = [
                    {
                        "calibration_note": (b["body"] or "")[:300],
                        "confidence": b["confidence"],
                        "token": f"b:{b['id']}",
                    }
                    for b in brows
                ]
            except Exception as e:
                log.warning("intro: blindspots failed: %s", e)
                brief["blindspots"] = []

            # STANDING TECH RULES: hard-won defaults every connecting
            # AI should apply. Full stories via GET /rules or the
            # cortex_rules MCP tool.
            try:
                brief["standing_tech_rules"] = db.rules_digest(limit=15)
            except Exception as e:
                log.warning("intro: tech rules failed: %s", e)
                brief["standing_tech_rules"] = []

            # INSTITUTIONAL MEMORY: most recent future_overseer_notes
            try:
                nrows = conn.execute(
                    "SELECT id, instance_id, written_at, body "
                    "FROM future_overseer_notes "
                    "ORDER BY written_at DESC LIMIT 3"
                ).fetchall()
                brief["recent_future_notes"] = [
                    {
                        "author": n["instance_id"],
                        "written_at": (n["written_at"] or "")[:10],
                        "excerpt": (n["body"] or "")[:280],
                        "token": f"n:{n['id']}",
                    }
                    for n in nrows
                ]
            except Exception as e:
                log.warning("intro: future_notes failed: %s", e)
                brief["recent_future_notes"] = []

            # OPS: single demoted sub-key with the operational
            # chatter. External AIs ignore this; the overseer and
            # looper still get it for their own use.
            try:
                ops = {
                    "journal_entry_count": conn.execute(
                        "SELECT COUNT(*) FROM overseer_journal"
                    ).fetchone()[0],
                    "future_overseer_notes_count": conn.execute(
                        "SELECT COUNT(*) FROM future_overseer_notes"
                    ).fetchone()[0],
                    "active_open_questions": conn.execute(
                        "SELECT COUNT(*) FROM open_questions "
                        "WHERE is_active = 1"
                    ).fetchone()[0],
                    "gist_count": conn.execute(
                        "SELECT COUNT(*) FROM summaries_gist"
                    ).fetchone()[0],
                }
                try:
                    ops["theme_gists_links"] = conn.execute(
                        "SELECT COUNT(*) FROM theme_gists"
                    ).fetchone()[0]
                except Exception:
                    ops["theme_gists_links"] = 0
                try:
                    ops["corpus_decisions_count"] = conn.execute(
                        "SELECT COUNT(*) FROM corpus_decisions"
                    ).fetchone()[0]
                except Exception:
                    ops["corpus_decisions_count"] = 0
                brief["ops"] = ops
            except Exception as e:
                log.warning("intro: ops failed: %s", e)
                brief["ops"] = {}

            fmt = str((payload or {}).get("format") or "").strip().lower()
            if fmt == "markdown":
                return {"ok": True,
                        "markdown": _render_intro_markdown(brief),
                        "brief": brief}
            return {"ok": True, "brief": brief}
        except Exception as e:
            log.exception("intro failed")
            return {"ok": False, "error": str(e)}

    def _http_f1_coverage(self, payload):
        """GET /plugins/overseer/f1-coverage

        Returns the F1 abstraction-graph coverage metric the looper
        pushed in cycle 2 (11.8% → 42.3% deterministically). Single-
        call read used by the deterministic loop + cycle 3 looper at
        boot to track the trend.

        Returns:
          total                — total gists in summaries_gist
          via_question         — distinct gists with evidence_for_question
          via_theme            — distinct gists in theme_gists
          via_either           — UNION (the F1 coverage metric)
          coverage_pct         — via_either / total as percentage
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            conn = self.overseer_db._conn
            total = conn.execute(
                "SELECT COUNT(*) FROM summaries_gist"
            ).fetchone()[0]
            via_q = conn.execute(
                "SELECT COUNT(DISTINCT evidence_id) "
                "FROM evidence_for_question "
                "WHERE evidence_table = 'summaries_gist'"
            ).fetchone()[0]
            # theme_gists may not exist on older installs (added by
            # the looper in cycle 2 iter 13); tolerate missing.
            try:
                via_t = conn.execute(
                    "SELECT COUNT(DISTINCT gist_id) FROM theme_gists"
                ).fetchone()[0]
                via_either = conn.execute(
                    "SELECT COUNT(DISTINCT id) FROM summaries_gist "
                    "WHERE id IN ("
                    "  SELECT evidence_id FROM evidence_for_question "
                    "  WHERE evidence_table='summaries_gist'"
                    ") OR id IN (SELECT gist_id FROM theme_gists)"
                ).fetchone()[0]
            except Exception:
                via_t = 0
                via_either = via_q
            pct = (100.0 * via_either / total) if total else 0.0
            return {
                "ok": True,
                "total": int(total),
                "via_question": int(via_q),
                "via_theme": int(via_t),
                "via_either": int(via_either),
                "coverage_pct": round(pct, 2),
            }
        except Exception as e:
            log.exception("f1_coverage failed")
            return {"ok": False, "error": str(e)}

    def _http_vault_render(self, payload):
        """POST /plugins/overseer/vault/render

        Body: {
          out_dir: "/abs/path/to/vault" (default below),
          gist_limit: int (0 = all, default 0)
        }

        Default out_dir resolves to the human user's home, not the
        service uid's home — the service runs as root on .25 so a
        bare ~/cortex-vault would land in /root/cortex-vault where
        the human user can't read it without sudo. L99 must-fix #3
        (2026-05-27).

        Renders the full vault. Returns counts + duration + any
        per-row errors. Synchronous — Phase 2.2 makes it async with
        progress polling.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        # Path precedence:
        #   1. payload.out_dir (explicit caller choice)
        #   2. plugin config vault_output_dir (per-Pi override)
        #   3. /home/turfptax/cortex-vault (hardcoded human-readable
        #      default on the .25 deploy)
        out_dir = str(
            (payload or {}).get("out_dir")
            or self.api.config.get("vault_output_dir")
            or "/home/turfptax/cortex-vault"
        )
        gist_limit = _as_int(payload, "gist_limit", 0,
                              max_value=10000)
        try:
            from vault_generator import render_vault
        except Exception as e:
            log.exception("vault_generator import failed")
            return {"ok": False,
                    "error": "vault_generator import failed: "
                              + str(e)}
        try:
            result = render_vault(
                self.overseer_db, out_dir,
                gist_limit=gist_limit,
                log_fn=log.info,
            )
            return result
        except Exception as e:
            log.exception("vault render failed")
            return {"ok": False,
                    "error": "vault render failed: " + str(e)}

    def _http_distill_corrections(self, payload):
        """POST /plugins/overseer/insight/distill-corrections

        Run a Sonnet pass over recent uncondidated corrections; propose
        blindspot candidates into pending_interpretations (kind=
        'blindspot') for human review. 3i CP2.
        """
        if self.overseer_db is None or self.llm is None:
            return {"ok": False, "error": "overseer not fully initialized"}
        max_cost = float(self.api.config.get(
            "insight_scan_max_cost_usd_per_scan", 0.05))
        try:
            return distill_uncondidated_corrections(
                db=self.overseer_db, llm=self.llm,
                max_cost_usd=max_cost,
                budget=None,                # manual; daily cap still enforced
                triggered_by="manual",
            )
        except Exception as e:
            log.exception("distill-corrections failed")
            return {"ok": False, "error": "distill failed: " + str(e)}

    def _http_route_existing_gists(self, payload):
        """POST /plugins/overseer/questions/route-existing

        Backfill route: for each gist in summaries_gist that has NOT
        been filed against any question yet, run question_routing.
        Bypasses per-tick budget; uses its own (typically larger).

        Body: {"limit": int (default 100), "max_cost_usd": float
               (default 0.50)}
        """
        if self.overseer_db is None or self.llm is None:
            return {"ok": False, "error": "overseer not fully initialized"}
        from question_routing import route_evidence_to_questions
        from loop import TickBudget
        limit = _as_int(payload, "limit", 100, max_value=2000)
        max_cost = float(payload.get("max_cost_usd", 0.50))
        budget = TickBudget(max_calls=limit, max_cost_usd=max_cost)
        # Find unrouted gists (all that have no row in evidence_for_question)
        rows = self.overseer_db._conn.execute(
            "SELECT g.id, g.body FROM summaries_gist g "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM evidence_for_question e "
            "  WHERE e.evidence_table = 'summaries_gist' "
            "  AND e.evidence_id = g.id"
            ") ORDER BY g.id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        results = {"processed": 0, "filed": 0, "unfiled": 0,
                   "errors": 0, "reactivated": []}
        for r in rows:
            if budget.exhausted():
                break
            try:
                rt = route_evidence_to_questions(
                    db=self.overseer_db, llm=self.llm,
                    gist_text=r["body"], gist_id=r["id"],
                    budget=budget, contributed_by="backfill",
                )
                results["processed"] += 1
                if rt.get("filings"):
                    results["filed"] += sum(
                        1 for f in rt["filings"] if f.get("newly_filed"))
                else:
                    results["unfiled"] += 1
                for q in rt.get("reactivated", []):
                    results["reactivated"].append(q)
            except Exception as e:
                results["errors"] += 1
                self.api.log.warning(
                    "backfill routing gist %s failed: %s", r["id"], e)
        results["budget"] = budget.remaining()
        results["total_unrouted"] = self.overseer_db._conn.execute(
            "SELECT COUNT(*) FROM summaries_gist g "
            "WHERE NOT EXISTS (SELECT 1 FROM evidence_for_question e "
            "WHERE e.evidence_table='summaries_gist' "
            "AND e.evidence_id=g.id)"
        ).fetchone()[0]
        return {"ok": True, **results}

    def _http_patterns(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 50, max_value=200)
        rows = self.overseer_db.recent_patterns(limit)
        for r in rows:
            r["tags"] = self.overseer_db.get_tags_for("patterns", r["id"])
        return {"ok": True, "patterns": rows}

    def _http_drift(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 50, max_value=200)
        rows = self.overseer_db.recent_drift(limit)
        for r in rows:
            r["tags"] = self.overseer_db.get_tags_for("drift_observations", r["id"])
        return {"ok": True, "drift": rows}

    def _http_future_notes(self, payload):
        """GET /plugins/overseer/future-notes — institutional memory of the
        overseer system itself, append-only, oldest first."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        return {"ok": True, "notes": self.overseer_db.all_future_notes()}

    # ── Slice 3c handlers ───────────────────────────────────────

    def _http_loop_status(self, payload):
        """GET /plugins/overseer/loop — background loop liveness + last tick."""
        if self.loop is None:
            return {"ok": False, "error": "loop not initialized"}
        s = self.loop.stats()
        return {"ok": True, **s}

    def _http_tick_now(self, payload):
        """POST /plugins/overseer/tick-now — run one tick immediately.

        Same work as the scheduled tick (summarize → tag → working memory),
        bound by the same per-tick budget. Useful for smoke testing without
        waiting `tick_interval_s` seconds.
        """
        if self.loop is None:
            return {"ok": False, "error": "loop not initialized"}
        try:
            summary = self.loop.run_one_tick(trigger="manual")
            return {"ok": summary.get("ok", True), "summary": summary}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _http_backfill(self, payload):
        """POST /plugins/overseer/backfill — process historical sessions/notes.

        Body (all optional):
            {"kind": "all"|"sessions"|"notes",
             "session_limit": int (default 200),
             "note_limit": int (default 500),
             "max_cost_usd": float (default 1.0),
             "max_calls": int (default heuristic)}

        Default budget is more generous than a tick ($1.00 vs $0.50) but
        still capped. To do a full sweep, pass max_cost_usd=10 (or more)
        and the corresponding limits.
        """
        if self.loop is None:
            return {"ok": False, "error": "loop not initialized"}
        kind = (payload.get("kind") or "all").lower()
        if kind not in ("all", "sessions", "notes", "imports"):
            return {"ok": False,
                    "error": "kind must be all|sessions|notes|imports"}
        try:
            session_limit = _as_int(payload, "session_limit", 200,
                                     max_value=10000)
            note_limit = _as_int(payload, "note_limit", 500,
                                  max_value=50000)
            max_cost_usd = float(payload.get("max_cost_usd", 1.0))
            max_calls = payload.get("max_calls")
            if max_calls is not None:
                max_calls = int(max_calls)
            summary = self.loop.backfill(
                kind=kind, session_limit=session_limit,
                note_limit=note_limit,
                max_cost_usd=max_cost_usd,
                max_calls=max_calls,
            )
            return {"ok": summary.get("ok", True), "summary": summary}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Slice 3d: import handlers ───────────────────────────────

    def _import_one_jsonl(self, src_path: Path, source: str) -> dict:
        """Shared ingest: copy file to plugin's imports dir, parse,
        upsert into imported_sessions. Returns a status dict.

        - Dedups by sha256: if (source, hash) already in imported_sessions,
          re-uses the existing row (no copy, no parse) and reports skipped.
        - Otherwise copies to plugins/overseer/data/imports/<source>/
          <session_id>.jsonl. Idempotent — re-importing the same file
          replaces the metadata row but keeps the existing imported_id.
        """
        if not src_path.is_file():
            return {"ok": False, "error": "file not found: {}".format(src_path)}
        if source != CLAUDE_CODE_SOURCE:
            return {"ok": False,
                    "error": "only claude-code source supported in slice 3d"}

        try:
            digest = file_sha256(src_path)
        except Exception as e:
            return {"ok": False, "error": "hash failed: {}".format(e)}

        # Dedup check
        existing = self.overseer_db.get_imported_by_hash(source, digest)
        if existing:
            return {
                "ok": True, "skipped": "already imported (same hash)",
                "imported_id": existing["id"], "file_hash": digest,
                "source_path": existing["source_path"],
            }

        # Parse
        try:
            metadata, messages = parse_claude_code_jsonl(src_path)
        except Exception as e:
            return {"ok": False,
                    "error": "parse failed for {}: {}".format(src_path, e)}

        session_id = (metadata.get("session_id")
                      or claude_code_session_id_from_path(src_path))
        imported_id = claude_code_imported_id(session_id)

        # Copy file into plugin-owned imports dir
        dest_dir = self.api.plugin_data / "imports" / source
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / "{}.jsonl".format(session_id)
        try:
            shutil.copy2(str(src_path), str(dest_path))
        except Exception as e:
            return {"ok": False, "error": "copy failed: {}".format(e)}
        try:
            bytes_size = dest_path.stat().st_size
        except Exception:
            bytes_size = 0

        # Project = canonical basename of cwd. The shared helper in
        # claude_jsonl.py is the SOLE source of truth — see
        # canonicalize_project_name() for behavior. Same helper is used
        # by the polish-slice migration that backfills any fossil
        # project tags from older code paths.
        project = canonicalize_project_name(metadata.get("cwd"))

        meta_extra = {
            "version": metadata.get("version"),
            "entrypoint": metadata.get("entrypoint"),
            "parse_errors": metadata.get("parse_errors"),
            "total_lines": metadata.get("total_lines"),
            "messages_captured": len(messages),
        }

        self.overseer_db.add_imported_session(
            id=imported_id,
            source=source,
            source_path=str(dest_path),
            project=project,
            cwd=metadata.get("cwd") or "",
            git_branch=metadata.get("git_branch") or "",
            started_at=metadata.get("started_at"),
            ended_at=metadata.get("ended_at"),
            duration_minutes=int(metadata.get("duration_minutes") or 0),
            message_count=int(metadata.get("message_count") or 0),
            user_message_count=int(metadata.get("user_message_count") or 0),
            assistant_message_count=int(
                metadata.get("assistant_message_count") or 0),
            tool_use_count=int(metadata.get("tool_use_count") or 0),
            bytes_size=bytes_size,
            file_hash=digest,
            metadata_json=json.dumps(meta_extra),
        )
        return {
            "ok": True, "imported_id": imported_id, "file_hash": digest,
            "source_path": str(dest_path),
            "session_id": session_id,
            "started_at": metadata.get("started_at"),
            "ended_at": metadata.get("ended_at"),
            "duration_minutes": metadata.get("duration_minutes"),
            "message_count": metadata.get("message_count"),
            "user_message_count": metadata.get("user_message_count"),
            "assistant_message_count": metadata.get("assistant_message_count"),
            "tool_use_count": metadata.get("tool_use_count"),
            "bytes_size": bytes_size,
            "cwd": metadata.get("cwd"),
            "git_branch": metadata.get("git_branch"),
            "project": project,
        }

    def _http_import_from_path(self, payload):
        """POST /plugins/overseer/imports/from-path

        Body: {"path": "/abs/path/to/session.jsonl",
               "source": "claude-code"}

        Pi-local path. Used by the Hub after uploading via /files/uploads.
        Idempotent — same content (sha256) imported twice is a no-op
        beyond updating metadata.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        path_str = (payload.get("path") or "").strip()
        if not path_str:
            return {"ok": False, "error": "missing 'path' field"}
        source = (payload.get("source") or CLAUDE_CODE_SOURCE).strip()
        return self._import_one_jsonl(Path(path_str), source)

    def _http_import_scan_dir(self, payload):
        """POST /plugins/overseer/imports/scan-dir

        Body: {"dir": "/abs/path/to/dir",
               "source": "claude-code",
               "recursive": true,
               "limit": 200}

        Walk a directory (default recursive) for *.jsonl files; import each
        one not already in imported_sessions (deduped by content hash).
        Useful for bulk import after the Hub uploads many files.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        dir_str = (payload.get("dir") or "").strip()
        if not dir_str:
            return {"ok": False, "error": "missing 'dir' field"}
        source = (payload.get("source") or CLAUDE_CODE_SOURCE).strip()
        recursive = str(payload.get("recursive", "true")).lower() not in (
            "0", "false", "no", "")
        limit = _as_int(payload, "limit", 200, max_value=10000)

        d = Path(dir_str)
        if not d.is_dir():
            return {"ok": False, "error": "dir not found: {}".format(d)}

        pattern = "**/*.jsonl" if recursive else "*.jsonl"
        candidates = sorted(d.glob(pattern))[:limit]

        results = {"imported": [], "skipped": [], "failed": []}
        for p in candidates:
            r = self._import_one_jsonl(p, source)
            entry = {
                "src": str(p),
                "imported_id": r.get("imported_id"),
                "error": r.get("error"),
            }
            if not r.get("ok"):
                results["failed"].append(entry)
            elif r.get("skipped"):
                entry["reason"] = r.get("skipped")
                results["skipped"].append(entry)
            else:
                results["imported"].append(entry)
        return {
            "ok": True,
            "scanned": len(candidates),
            "imported_count": len(results["imported"]),
            "skipped_count": len(results["skipped"]),
            "failed_count": len(results["failed"]),
            "details": results,
        }

    def _http_list_imports(self, payload):
        """GET /plugins/overseer/imports?source=&limit=N&offset=N"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        source = (payload.get("source") or "").strip() or None
        limit = _as_int(payload, "limit", 100, max_value=2000)
        offset = _as_int(payload, "offset", 0, max_value=100000)
        rows = self.overseer_db.list_imported_sessions(
            source=source, limit=limit, offset=offset)
        # Decorate each row with whether it's been processed yet
        for r in rows:
            r["processed"] = self.overseer_db.is_imported_processed(r["id"])
        return {
            "ok": True,
            "imports": rows,
            "total": self.overseer_db.imported_session_count(source=source),
        }

    def _http_delete_import(self, payload):
        """POST /plugins/overseer/imports/delete

        Body: {"id": "claude-code:<uuid>", "remove_file": true}

        Removes the imported_sessions row + the corresponding
        processed_imported_sessions row + (optionally) the .jsonl file
        on Pi disk. Does NOT delete any summaries_gist row that was
        produced from this import — those persist as derived data.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        imp_id = (payload.get("id") or "").strip()
        if not imp_id:
            return {"ok": False, "error": "missing 'id' field"}
        remove_file = str(payload.get("remove_file", "true")).lower() not in (
            "0", "false", "no", "")

        existing = self.overseer_db.get_imported_by_id(imp_id)
        if not existing:
            return {"ok": False, "error": "no such import: {}".format(imp_id)}

        file_removed = False
        if remove_file and existing.get("source_path"):
            try:
                p = Path(existing["source_path"])
                if p.is_file():
                    p.unlink()
                    file_removed = True
            except Exception as e:
                self.api.log.warning(
                    "could not remove imported file %s: %s",
                    existing.get("source_path"), e)

        n = self.overseer_db.delete_imported_session(imp_id)
        return {"ok": True, "deleted": n > 0, "file_removed": file_removed}

    def _http_working_memory(self, payload):
        """GET /plugins/overseer/working-memory — cached artifact.

        Returns the most-recent built artifact straight from
        overseer_state.working_memory_json (zero-latency read; no LLM call).
        Pass ?rebuild=1 to force a fresh build before returning.
        """
        if self.overseer_db is None or self.loop is None:
            return {"ok": False, "error": "overseer not fully initialized"}
        rebuild = str(payload.get("rebuild", "")).lower() in ("1", "true", "yes")
        if rebuild:
            try:
                wm = self.loop.build_working_memory()
                self.overseer_db.set_overseer_state(
                    "working_memory_json", json.dumps(wm))
                self.overseer_db.set_overseer_state(
                    "working_memory_built_at",
                    wm.get("built_at"))
                return {"ok": True, "working_memory": wm,
                        "source": "rebuilt"}
            except Exception as e:
                return {"ok": False, "error": "rebuild failed: " + str(e)}

        cached = self.overseer_db.get_overseer_state("working_memory_json")
        if not cached:
            return {"ok": True, "working_memory": None,
                    "source": "empty",
                    "hint": "call POST /tick-now or GET /working-memory?rebuild=1"}
        try:
            wm = json.loads(cached)
        except Exception as e:
            return {"ok": False, "error": "cached wm corrupt: " + str(e)}
        # Slice 9.2 (overseer ask #2): surface built_at + age_minutes at
        # the top level too, so any /working-memory consumer (Hub UI,
        # MCP, sibling-Claude check-in scripts) has the same staleness
        # signal the chat-context path now exposes.
        built_at = wm.get("built_at") or self.overseer_db.get_overseer_state(
            "working_memory_built_at")
        age_minutes = None
        if built_at:
            try:
                from datetime import datetime, timezone
                b = datetime.fromisoformat(built_at.replace("Z", "+00:00"))
                age_minutes = max(
                    0,
                    int((datetime.now(timezone.utc) - b).total_seconds() / 60),
                )
            except Exception:
                age_minutes = None
        return {
            "ok": True, "working_memory": wm, "source": "cache",
            "working_memory_built_at": built_at,
            "working_memory_age_minutes": age_minutes,
        }


    # ── Slice 3e handlers ───────────────────────────────────────

    def _http_list_projects(self, payload):
        """GET /plugins/overseer/projects — per-project classification +
        per-project counts. Combines imported_project_settings with live
        counts from imported_sessions.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        rows = self.overseer_db._conn.execute(
            "SELECT project, COUNT(*) AS n, "
            "AVG(duration_minutes) AS avg_min, "
            "AVG(message_count) AS avg_msg, "
            "MAX(started_at) AS last_seen, "
            "SUM(message_count) AS total_msgs "
            "FROM imported_sessions GROUP BY project "
            "ORDER BY n DESC"
        ).fetchall()
        out = []
        for r in rows:
            project = r["project"] or ""
            setting = self.overseer_db.get_project_setting(project)
            out.append({
                "project": project,
                "session_count": r["n"],
                "avg_duration_minutes": round(r["avg_min"] or 0, 1),
                "avg_messages": round(r["avg_msg"] or 0, 1),
                "total_messages": r["total_msgs"] or 0,
                "last_seen": r["last_seen"],
                "treat_as": setting.get("treat_as", "auto"),
                "manual_override": bool(setting.get("manual_override")),
                "classified_at": setting.get("classified_at"),
                "classified_reason": setting.get("classified_reason"),
                "rollup_count": self.overseer_db._conn.execute(
                    "SELECT COUNT(*) FROM automation_rollups "
                    "WHERE project = ?", (project,)
                ).fetchone()[0],
            })
        return {"ok": True, "projects": out, "total": len(out)}

    def _http_classify_now(self, payload):
        """POST /plugins/overseer/projects/classify — run auto-classifier
        across all imported projects right now and return the changes."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        try:
            changes = self.overseer_db.auto_classify_projects()
            return {"ok": True, "changes": changes}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _http_set_project_setting(self, payload):
        """POST /plugins/overseer/projects/setting

        Body: {"project": "...", "treat_as": "auto|human|automation|ignore"}
        Sets the manual_override flag automatically — auto-classifier
        won't change this project until manual_override is cleared."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        project = (payload.get("project") or "").strip()
        treat_as = (payload.get("treat_as") or "").strip()
        if not project:
            return {"ok": False, "error": "missing 'project'"}
        if treat_as not in ("auto", "human", "automation", "ignore"):
            return {"ok": False,
                    "error": "treat_as must be auto|human|automation|ignore"}
        # treat_as=auto means: clear manual override, let classifier decide
        manual = treat_as != "auto"
        try:
            self.overseer_db.set_project_setting(
                project, treat_as=treat_as,
                manual_override=manual,
                classified_reason="user override")
            return {"ok": True,
                    "setting": self.overseer_db.get_project_setting(project)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _http_list_rollups(self, payload):
        """GET /plugins/overseer/rollups?project=&limit=N"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        project = (payload.get("project") or "").strip() or None
        limit = _as_int(payload, "limit", 100, max_value=2000)
        rollups = self.overseer_db.list_rollups(
            project=project, limit=limit)
        return {"ok": True, "rollups": rollups}

    def _http_chat(self, payload):
        """POST /plugins/overseer/chat

        Body: {"message": "...", "backend"?: "openrouter|lmstudio|ondevice",
               "max_tokens"?: int, "temperature"?: float,
               "attachments"?: [
                   {filename, mime_type, size, pi_path,
                    file_id?, sha256?}, ...
               ]}

        Slice 8: when attachments are passed, each pi_path must already
        exist on disk under UPLOADS_DIR (the Hub uploaded them via
        /files/uploads first). Text/pdf contents are inlined into the
        prompt; images go through the multimodal channel; everything
        is persisted to chat_message_files keyed to the user turn.
        Allowing message="" + attachments-only — see respond_to_message.
        """
        if (self.overseer_db is None or self.llm is None
                or self.core_memory is None):
            return {"ok": False, "error": "overseer not initialized"}
        message = payload.get("message") or ""
        attachments = payload.get("attachments") or []
        if not isinstance(attachments, list):
            return {"ok": False,
                    "error": "'attachments' must be a list of file refs"}
        # Defense in depth on top of the Hub's allowlist.
        if len(attachments) > 10:
            return {"ok": False,
                    "error": "too many attachments (max 10 per turn)"}
        if not message.strip() and not attachments:
            return {"ok": False, "error": "missing 'message' field"}
        try:
            from config import UPLOADS_DIR
        except Exception:
            UPLOADS_DIR = None
        try:
            return respond_to_message(
                db=self.overseer_db, llm=self.llm,
                core_memory=self.core_memory,
                user_message=message.strip(),
                backend=payload.get("backend"),
                max_tokens=_as_int(payload, "max_tokens", 64000, 128000),
                temperature=float(payload.get("temperature", 0.7)),
                max_history_turns=_as_int(
                    payload, "max_history_turns", 20, 100),
                insight_snippet_enabled=bool(self.api.config.get(
                    "insight_chat_snippet_enabled", True)),
                attachments=attachments,
                uploads_dir=UPLOADS_DIR,
                # Slice 9.3: cap on dispatch_sibling calls from chat tools
                sibling_daily_cap=self._sibling_daily_cap(),
                # Slice 14: voice-mode succinctness directive
                voice_mode=bool(payload.get("voice_mode", False)),
                # Agent harness: the Hub pins the thread it rendered
                # so a pointer move mid-request can't redirect the
                # turn. 0/absent = active thread.
                thread_id=_as_int(payload, "thread_id", 0) or None,
            )
        except Exception as e:
            self.api.log.exception("chat failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _http_quick_chat(self, payload):
        """Slice 14.7 (2026-05-22): POST /plugins/overseer/quick-chat

        Router-tier chat. Persists the user message once, then either:
          - answers via Gemini Flash with thin context (~$0.0003-0.001)
          - or escalates to the full respond_to_message path (Opus,
            full context, ~$0.10-0.15) when the question needs it

        Body: {"message": "...", "direct_override"?: bool}
        Returns the same shape as /chat, plus:
          - answered_by: 'router' | 'overseer'
          - escalation_reason: '' or one of the tagged reasons
          - router_attempted: bool
        """
        if (self.overseer_db is None or self.llm is None
                or self.core_memory is None):
            return {"ok": False, "error": "overseer not initialized"}
        message = (payload.get("message") or "").strip()
        if not message:
            return {"ok": False, "error": "missing 'message' field"}
        direct_override = bool(payload.get("direct_override", False))
        try:
            return respond_via_router(
                db=self.overseer_db, llm=self.llm,
                core_memory=self.core_memory,
                user_message=message,
                direct_override=direct_override,
                sibling_daily_cap=self._sibling_daily_cap(),
                thread_id=_as_int(payload, "thread_id", 0) or None,
            )
        except Exception as e:
            self.api.log.exception("quick-chat failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _http_chat_history(self, payload):
        """GET /plugins/overseer/chat/history?limit=N&thread_id=N

        Slice 8: each message dict carries an `attachments` list (empty
        if no files were attached) so the frontend can re-render
        thumbnails after a reload.

        Agent harness: thread_id is optional — omitted means the
        active thread. Passing it lets the Hub browse any thread
        read-only without moving the active pointer; sends always
        target the active thread (switch via /chat/threads/select)."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 50, max_value=500)
        if limit < 1:
            # A negative LIMIT means 'no limit' to SQLite — don't let
            # a crafted request bypass the 500-row cap.
            limit = 50
        thread_id = _as_int(payload, "thread_id", 0) or None
        active_id = self.overseer_db.active_chat_thread_id()
        return {
            "ok": True,
            "messages": self.overseer_db.recent_chat_messages(
                limit, thread_id=thread_id),
            "total": self.overseer_db.chat_message_count(thread_id),
            "thread_id": thread_id or active_id,
            "active_thread_id": active_id,
        }

    def _http_chat_clear(self, payload):
        """POST /plugins/overseer/chat/clear — wipe one chat thread's
        messages (thread row survives). Per locked design, append-only
        is for future_overseer_notes; chat is a working thread the
        user can reset.

        Agent harness: the Hub pins thread_id to the thread it is
        RENDERING; resolving the active pointer at request time could
        wipe a different thread if the pointer moved while the confirm
        dialog was open."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        tid = (_as_int(payload, "thread_id", 0)
               or self.overseer_db.active_chat_thread_id())
        n = self.overseer_db.chat_message_count(tid)
        self.overseer_db.clear_chat(tid)
        return {"ok": True, "cleared": n, "thread_id": tid}

    def _http_chat_compress(self, payload):
        """POST /plugins/overseer/chat/compress
        Body: {"keep_recent"?: int}  default 12

        Slice 9.5 CP3: fold older chat turns into a Sonnet-generated
        summary so the recent conversation has continuity without
        paying for the full thread every turn. Surface for both Tory
        (via /compress slash command) and the overseer (via the
        compress_chat tool — see chat_tools.py)."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        if self.llm is None:
            return {"ok": False, "error": "llm not initialized"}
        keep_recent = int(payload.get("keep_recent") or 12)
        try:
            import chat as _chat_mod
            return _chat_mod.compress_chat_history(
                db=self.overseer_db,
                llm=self.llm,
                keep_recent=keep_recent,
                thread_id=_as_int(payload, "thread_id", 0) or None,
            )
        except Exception as e:
            log.exception("chat/compress failed")
            return {"ok": False, "error": str(e)[:500]}

    # ── Agent harness (2026-07-10): chat threads ─────────────────
    # Sends (/chat, /quick-chat) always target the ACTIVE thread;
    # the Hub switches threads via /chat/threads/select first. This
    # keeps every pre-thread consumer (voice mode, MCP overseer_chat,
    # the compress_chat tool, the router streak) coherent for free.

    def _http_chat_threads(self, payload):
        """GET /plugins/overseer/chat/threads"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        return {
            "ok": True,
            "threads": self.overseer_db.list_chat_threads(),
            "active_thread_id":
                self.overseer_db.active_chat_thread_id(),
        }

    def _http_chat_thread_new(self, payload):
        """POST /plugins/overseer/chat/threads/new  {title?}
        Creates + selects the new thread."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        title = payload.get("title") or ""
        if not isinstance(title, str):
            return {"ok": False, "error": "'title' must be a string"}
        tid = self.overseer_db.create_chat_thread(title)
        return {"ok": True, "thread_id": tid}

    def _http_chat_thread_select(self, payload):
        """POST /plugins/overseer/chat/threads/select  {thread_id}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        tid = _as_int(payload, "thread_id", 0)
        if not tid:
            return {"ok": False, "error": "missing 'thread_id'"}
        if not self.overseer_db.select_chat_thread(tid):
            return {"ok": False, "error": f"no such thread: {tid}"}
        return {"ok": True, "thread_id": tid}

    def _http_chat_thread_rename(self, payload):
        """POST /plugins/overseer/chat/threads/rename
        {thread_id, title}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        tid = _as_int(payload, "thread_id", 0)
        title = payload.get("title") or ""
        if not isinstance(title, str):
            return {"ok": False, "error": "'title' must be a string"}
        title = title.strip()
        if not tid or not title:
            return {"ok": False, "error": "need 'thread_id' + 'title'"}
        if not self.overseer_db.rename_chat_thread(tid, title):
            return {"ok": False, "error": f"no such thread: {tid}"}
        return {"ok": True, "thread_id": tid, "title": title[:120]}

    def _http_chat_thread_delete(self, payload):
        """POST /plugins/overseer/chat/threads/delete  {thread_id}
        Deletes the thread + its messages. The active pointer heals
        to the most recent remaining thread on next use."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        tid = _as_int(payload, "thread_id", 0)
        if not tid:
            return {"ok": False, "error": "missing 'thread_id'"}
        if not self.overseer_db.delete_chat_thread(tid):
            return {"ok": False, "error": f"no such thread: {tid}"}
        return {"ok": True, "deleted": tid,
                "active_thread_id":
                    self.overseer_db.active_chat_thread_id()}

    # ── Agent harness: prompt library ────────────────────────────

    def _http_chat_prompts(self, payload):
        """GET /plugins/overseer/chat/prompts"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        return {"ok": True,
                "prompts": self.overseer_db.list_chat_prompts()}

    def _http_chat_prompt_upsert(self, payload):
        """POST /plugins/overseer/chat/prompts/upsert
        {id?, title, body} — id present = update, absent = create."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        title = payload.get("title") or ""
        body = payload.get("body") or ""
        if not isinstance(title, str) or not isinstance(body, str):
            return {"ok": False,
                    "error": "'title' and 'body' must be strings"}
        # An id that was SENT but doesn't parse must error, not
        # silently fall through to create-a-duplicate.
        raw_id = payload.get("id")
        if raw_id in (None, "", 0):
            prompt_id = None
        else:
            try:
                prompt_id = int(raw_id)
            except (TypeError, ValueError):
                return {"ok": False,
                        "error": f"invalid 'id': {raw_id!r}"}
        pid = self.overseer_db.upsert_chat_prompt(
            prompt_id=prompt_id,
            title=title,
            body=body,
        )
        if not pid:
            return {"ok": False,
                    "error": "need non-empty 'title' + 'body' "
                             "(or id not found)"}
        return {"ok": True, "id": pid}

    def _http_chat_prompt_delete(self, payload):
        """POST /plugins/overseer/chat/prompts/delete  {id}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        pid = _as_int(payload, "id", 0)
        if not pid:
            return {"ok": False, "error": "missing 'id'"}
        if not self.overseer_db.delete_chat_prompt(pid):
            return {"ok": False, "error": f"no such prompt: {pid}"}
        return {"ok": True, "deleted": pid}

    # ── Agent harness (2026-07-11): harness map ──────────────────

    def _http_harness_map(self, payload):
        """GET /plugins/overseer/harness-map — the single succinct
        map of every screen/feature. Updated with every feature."""
        import chat_tools as _ct
        text = _ct.read_harness_map()
        if not text:
            return {"ok": False, "error": "HARNESS_MAP.md not found"}
        return {"ok": True, "map": text}

    # ── Agent harness (2026-07-11): interaction meta-feedback ────
    # Note-first by design (Tory: lightweight; "Discuss with
    # Overseer" is the SECONDARY option). Discuss threads MUST carry
    # the full context of what was rated (context injection rule).

    FEEDBACK_KINDS = ("chat_turn", "chat_thread", "voice_chat",
                      "bell_notification", "dispatch", "screen")

    def _http_feedback_list(self, payload):
        """GET /plugins/overseer/feedback?limit=N&target_kind="""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 50, max_value=500)
        kind = (payload.get("target_kind") or "").strip() or None
        return {"ok": True,
                "feedback": self.overseer_db.list_interaction_feedback(
                    limit=max(1, limit), target_kind=kind)}

    def _http_feedback_add(self, payload):
        """POST /plugins/overseer/feedback
        {target_kind, target_id?, rating?, note?, context?, source?}
        rating in (-1, 0, 1); note-only rows use rating 0."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        kind = (payload.get("target_kind") or "").strip()
        if kind not in self.FEEDBACK_KINDS:
            return {"ok": False,
                    "error": "target_kind must be one of: "
                             + ", ".join(self.FEEDBACK_KINDS)}
        rating = _as_int(payload, "rating", 0)
        if rating not in (-1, 0, 1):
            return {"ok": False, "error": "rating must be -1, 0 or 1"}
        note = payload.get("note") or ""
        if not isinstance(note, str):
            return {"ok": False, "error": "'note' must be a string"}
        if rating == 0 and not note.strip():
            return {"ok": False,
                    "error": "give a rating, a note, or both"}
        context = payload.get("context")
        if context is not None and not isinstance(context, dict):
            return {"ok": False, "error": "'context' must be an object"}
        source = payload.get("source") or "hub"
        if not isinstance(source, str):
            source = "hub"
        fid = self.overseer_db.add_interaction_feedback(
            target_kind=kind,
            target_id=str(payload.get("target_id") or ""),
            rating=rating,
            note=note,
            context=context,
            source=source)
        return {"ok": True, "id": fid}

    def _http_feedback_discuss(self, payload):
        """POST /plugins/overseer/feedback/discuss  {feedback_id}

        The SECONDARY escalation: opens (or re-opens) a chat thread
        seeded with the full context of the rated interaction, per
        Tory's context-injection rule. Returns thread_id; the client
        navigates to chat."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        fid = _as_int(payload, "feedback_id", 0)
        if not fid:
            return {"ok": False, "error": "missing 'feedback_id'"}
        fb = self.overseer_db.get_interaction_feedback(fid)
        if not fb:
            return {"ok": False, "error": f"no such feedback: {fid}"}

        # Idempotent re-open: if a discuss thread already exists,
        # select it instead of minting another. If /chat/clear wiped
        # it, re-seed the context so the reopened thread is never
        # context-less (the context-injection rule is hard).
        context_block = self._feedback_context_block(fb)
        if fb.get("meta_thread_id"):
            if self.overseer_db.select_chat_thread(
                    fb["meta_thread_id"]):
                if self.overseer_db.chat_message_count(
                        fb["meta_thread_id"]) == 0:
                    self.overseer_db.append_chat_message(
                        role="system", content=context_block,
                        backend="feedback-discuss",
                        thread_id=fb["meta_thread_id"])
                return {"ok": True, "thread_id": fb["meta_thread_id"],
                        "reopened": True}

        title = "Feedback: " + (
            (fb.get("note") or "").strip().split("\n")[0][:40]
            or f"{fb['target_kind']} #{fb['target_id']}")
        tid = self.overseer_db.create_chat_thread(title)
        self.overseer_db.append_chat_message(
            role="system", content=context_block,
            backend="feedback-discuss", thread_id=tid)
        if not self.overseer_db.set_feedback_thread(fid, tid):
            # Lost a concurrent-discuss race: another request already
            # linked a thread. Drop ours and join the winner's.
            winner = self.overseer_db.get_interaction_feedback(fid)
            winner_tid = (winner or {}).get("meta_thread_id")
            if winner_tid and winner_tid != tid:
                self.overseer_db.delete_chat_thread(tid)
                self.overseer_db.select_chat_thread(winner_tid)
                return {"ok": True, "thread_id": winner_tid,
                        "reopened": True}
        return {"ok": True, "thread_id": tid, "reopened": False}

    def _feedback_context_block(self, fb) -> str:
        """Context injection for a discuss thread: everything the
        overseer needs to know what Tory was looking at and what he
        said about it. Includes the rated exchange for chat turns."""
        rating_word = {1: "GOOD (thumbs up)",
                       -1: "BAD (thumbs down)"}.get(
            fb.get("rating") or 0, "note only (no rating)")
        lines = [
            "**[Meta-feedback discussion]**",
            "",
            "Tory left feedback on an AI interaction and chose to "
            "discuss it with you. Be direct about what went wrong or "
            "right and what to change; this feeds Cortex development. "
            "Call get_harness_map if you need to place the surface he "
            "was using.",
            "",
            f"- Target: {fb.get('target_kind')} #{fb.get('target_id')}",
            f"- Rating: {rating_word}",
            f"- Source: {fb.get('source')} at {fb.get('created_at')}",
        ]
        note = (fb.get("note") or "").strip()
        if note:
            lines += ["", "His note:", "> " + note.replace("\n", "\n> ")]
        ctx = _safe_json_loads(fb.get("context_json"), {})
        if ctx:
            lines += ["", "Screen context (from the client):",
                      "```json",
                      json.dumps(ctx, indent=2, default=str)[:2000],
                      "```"]
        # For chat turns, inject the rated exchange itself.
        if fb.get("target_kind") == "chat_turn":
            try:
                row = self.overseer_db._conn.execute(
                    "SELECT * FROM chat_messages WHERE id = ?",
                    (int(fb.get("target_id") or 0),)).fetchone()
            except (TypeError, ValueError):
                row = None
            if row:
                row = dict(row)
                user_row = self.overseer_db._conn.execute(
                    "SELECT content FROM chat_messages "
                    "WHERE role = 'user' AND thread_id = ? AND id < ? "
                    "ORDER BY id DESC LIMIT 1",
                    (row.get("thread_id") or 0, row["id"])).fetchone()
                lines += ["", "The rated exchange:"]
                if user_row:
                    lines += ["", "USER:",
                              (user_row["content"] or "")[:1500]]
                lines += ["", f"ASSISTANT ({row.get('model') or '?'}, "
                              f"answered_by={row.get('answered_by') or '?'}):",
                          (row.get("content") or "")[:2500]]
        return "\n".join(lines)

    def _http_feedback_summary(self, payload):
        """GET /plugins/overseer/feedback/summary — the Squeeze report
        card's conversations section: totals, by target kind, by model
        (chat turns join chat_messages; voice chats carry the model in
        their context), and the most recent notes (note-first: the
        text is the signal)."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        rows = self.overseer_db.list_interaction_feedback(limit=500)
        up = down = notes = 0
        by_kind: dict = {}
        by_model: dict = {}
        # Batch-resolve models for chat_turn targets in one query.
        turn_ids = []
        for r in rows:
            if r.get("target_kind") == "chat_turn":
                try:
                    turn_ids.append(int(r.get("target_id") or 0))
                except (TypeError, ValueError):
                    pass
        turn_models: dict = {}
        if turn_ids:
            marks = ",".join("?" * len(turn_ids))
            for mrow in self.overseer_db._conn.execute(
                    f"SELECT id, model FROM chat_messages "
                    f"WHERE id IN ({marks})", turn_ids).fetchall():
                turn_models[mrow["id"]] = mrow["model"] or ""
        recent = []
        for r in rows:
            rating = int(r.get("rating") or 0)
            if rating > 0:
                up += 1
            elif rating < 0:
                down += 1
            if (r.get("note") or "").strip():
                notes += 1
            kind = r.get("target_kind") or "?"
            k = by_kind.setdefault(kind, {"up": 0, "down": 0, "n": 0})
            k["n"] += 1
            if rating > 0:
                k["up"] += 1
            elif rating < 0:
                k["down"] += 1
            # Model attribution
            model = ""
            if kind == "chat_turn":
                try:
                    model = turn_models.get(int(r.get("target_id")), "")
                except (TypeError, ValueError):
                    model = ""
            else:
                ctx = _safe_json_loads(r.get("context_json"), {})
                model = str(ctx.get("model") or "")
            if model:
                m = by_model.setdefault(
                    model, {"up": 0, "down": 0, "n": 0})
                m["n"] += 1
                if rating > 0:
                    m["up"] += 1
                elif rating < 0:
                    m["down"] += 1
            if len(recent) < 30:
                recent.append({
                    "id": r.get("id"),
                    "target_kind": kind,
                    "target_id": r.get("target_id"),
                    "rating": rating,
                    "note": (r.get("note") or "")[:280],
                    "model": model,
                    "source": r.get("source"),
                    "meta_thread_id": r.get("meta_thread_id"),
                    "created_at": r.get("created_at"),
                })
        return {"ok": True,
                "totals": {"count": len(rows), "up": up, "down": down,
                           "with_notes": notes},
                "by_kind": by_kind,
                "by_model": by_model,
                "recent": recent}

    # ── Simples mirror (2026-07-11) ──────────────────────────────
    # The phone's liquid planner, mirrored for the desktop's read-only
    # Simples page. Display state, NOT corpus content (the planner's
    # no-corpus decision from 2026-07-02 stands): one replaceable blob
    # in overseer_state, phone authoritative, last write wins.

    def _http_simples_snapshot_post(self, payload):
        """POST /plugins/overseer/simples/snapshot
        {goals: [...], blocks: [...], from, to}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        goals = payload.get("goals")
        blocks = payload.get("blocks")
        if not isinstance(goals, list) or not isinstance(blocks, list):
            return {"ok": False,
                    "error": "'goals' and 'blocks' must be lists"}
        if len(goals) > 500 or len(blocks) > 5000:
            return {"ok": False, "error": "snapshot too large"}
        from datetime import datetime, timezone
        snap = {
            "goals": goals,
            "blocks": blocks,
            "from": str(payload.get("from") or "")[:10],
            "to": str(payload.get("to") or "")[:10],
            "received_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"),
        }
        self.overseer_db.set_overseer_state(
            "simples_snapshot_json",
            json.dumps(snap, ensure_ascii=False, default=str))
        self.overseer_db._safe_commit()
        return {"ok": True, "goals": len(goals), "blocks": len(blocks)}

    def _http_simples_snapshot_get(self, payload):
        """GET /plugins/overseer/simples/snapshot"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        raw = self.overseer_db.get_overseer_state("simples_snapshot_json")
        return {"ok": True,
                "snapshot": _safe_json_loads(raw, None) if raw else None}

    # ── Agent harness (2026-07-11): MCP connectors ───────────────

    def _http_mcp_connectors(self, payload):
        """GET /plugins/overseer/mcp/connectors — auth values are
        never echoed back, only whether one is set."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        rows = self.overseer_db.list_mcp_connectors()
        for r in rows:
            r["has_auth"] = bool(r.pop("auth_header", ""))
        return {"ok": True, "connectors": rows}

    def _http_mcp_connector_upsert(self, payload):
        """POST /plugins/overseer/mcp/connectors/upsert
        {name, base_url, auth_header?, enabled?}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        import mcp_client as _mcp
        name = payload.get("name") or ""
        base_url = payload.get("base_url") or ""
        if not isinstance(name, str) or not isinstance(base_url, str):
            return {"ok": False,
                    "error": "'name' and 'base_url' must be strings"}
        if not _mcp.validate_slug(name):
            return {"ok": False,
                    "error": "name must be a short slug: "
                             "[a-z0-9][a-z0-9-]{0,31}"}
        if not base_url.strip().startswith(("http://", "https://")):
            return {"ok": False,
                    "error": "base_url must be http(s)"}
        # Absent key = PRESERVE the stored secret (the list route
        # masks it, so a UI edit can't round-trip it); '' = clear.
        auth = payload.get("auth_header", None)
        if auth is not None and not isinstance(auth, str):
            return {"ok": False,
                    "error": "'auth_header' must be a string"}
        cid = self.overseer_db.upsert_mcp_connector(
            name=name, base_url=base_url, auth_header=auth,
            enabled=bool(payload.get("enabled", True)))
        return {"ok": True, "id": cid,
                "name": name.strip().lower()}

    def _http_mcp_connector_delete(self, payload):
        """POST /plugins/overseer/mcp/connectors/delete  {name}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        name = payload.get("name") or ""
        if not self.overseer_db.delete_mcp_connector(name):
            return {"ok": False, "error": f"no such connector: {name}"}
        return {"ok": True, "deleted": name}

    def _http_mcp_connector_test(self, payload):
        """POST /plugins/overseer/mcp/connectors/test  {name} —
        forces a fresh handshake + tools/list."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        import mcp_client as _mcp
        return _mcp.test_connector(self.overseer_db,
                                   payload.get("name") or "")

    def _http_notifications(self, payload):
        """GET /plugins/overseer/notifications

        Query params (all optional):
          include_dismissed=1   surface dismissed too
          include_archived=1    surface archived too
          include_snoozed=1     surface snoozed-not-yet-due too
          limit=N               default 100
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        truthy = lambda k: str(payload.get(k, "")).lower() in (
            "1", "true", "yes")
        limit = _as_int(payload, "limit", 100, max_value=1000)
        return {
            "ok": True,
            "notifications": self.overseer_db.list_notifications(
                include_dismissed=truthy("include_dismissed"),
                include_archived=truthy("include_archived"),
                include_snoozed=truthy("include_snoozed"),
                limit=limit),
            "unread_count":
                self.overseer_db.unread_notification_count(),
        }

    def _http_notifications_dismiss(self, payload):
        """POST /plugins/overseer/notifications/dismiss

        Body: {"id": int}  → dismiss one
              {"all": true} → dismiss every unread
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        if payload.get("all"):
            n = self.overseer_db.dismiss_all_notifications()
            return {"ok": True, "dismissed": n}
        nid = payload.get("id")
        if nid is None:
            return {"ok": False, "error": "pass id or all=true"}
        try:
            ok = self.overseer_db.dismiss_notification(int(nid))
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be an integer"}
        return {"ok": ok, "dismissed": ok}

    def _http_notifications_action(self, payload):
        """POST /plugins/overseer/notifications/action  (3i CP1)

        Body: {"id": int, "action": "archive"|"snooze"|"touch",
               "snooze_days"?: int}
        snooze_days defaults to 30 when action='snooze'.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        nid_raw = payload.get("id")
        if nid_raw is None:
            return {"ok": False, "error": "id is required"}
        try:
            nid = int(nid_raw)
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be an integer"}
        action = str(payload.get("action") or "").strip().lower()

        if action == "archive":
            ok = self.overseer_db.archive_notification(nid)
            return {"ok": ok, "action": "archive", "id": nid}
        if action == "touch":
            ok = self.overseer_db.touch_notification(nid)
            return {"ok": ok, "action": "touch", "id": nid}
        if action == "snooze":
            days = _as_int(payload, "snooze_days", 30, max_value=365)
            from datetime import datetime, timedelta, timezone
            until = (datetime.now(timezone.utc)
                     + timedelta(days=days)).strftime(
                         "%Y-%m-%d %H:%M:%S")
            ok = self.overseer_db.snooze_notification(nid, until)
            return {
                "ok": ok, "action": "snooze",
                "id": nid, "snoozed_until": until,
                "snooze_days": days,
            }
        return {"ok": False, "error": "action must be archive | snooze | touch"}

    def _http_notifications_respond(self, payload):
        """POST /plugins/overseer/notifications/respond  (Slice 9.6 CP1)

        Body: {"notification_id": int, "action_kind": str,
               "action_label"?: str, "response_payload"?: dict,
               "also_archive"?: bool}

        Logs Tory's response to a custom action button. Returns the new
        notification_responses.id. If also_archive is true (default),
        the notification is archived in the same call — most action
        responses imply the user has handled the notification.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        nid_raw = payload.get("notification_id")
        if nid_raw is None:
            return {"ok": False, "error": "notification_id is required"}
        try:
            nid = int(nid_raw)
        except (TypeError, ValueError):
            return {"ok": False, "error": "notification_id must be int"}
        kind = str(payload.get("action_kind") or "").strip()
        if not kind:
            return {"ok": False, "error": "action_kind is required"}
        label = str(payload.get("action_label") or "")
        response_payload = payload.get("response_payload") or {}
        if not isinstance(response_payload, dict):
            return {"ok": False, "error": "response_payload must be object"}
        also_archive = bool(payload.get("also_archive", True))
        try:
            resp_id = self.overseer_db.add_notification_response(
                notification_id=nid,
                action_kind=kind,
                action_label=label,
                response_payload=response_payload,
            )
        except Exception as e:
            log.exception("notifications/respond failed")
            return {"ok": False, "error": str(e)[:300]}
        archived = False
        if also_archive:
            try:
                archived = self.overseer_db.archive_notification(nid)
            except Exception:
                pass
        return {
            "ok": True, "response_id": resp_id,
            "notification_id": nid, "action_kind": kind,
            "archived": archived,
        }

    # ── Slice 3f: dialectic handlers ────────────────────────────

    def _http_list_dialectic(self, payload):
        """GET /plugins/overseer/dialectic
        Filters: status (open|resolved|productive), severity
        (none|minor|significant), artifact_type (gist|theme|episode|question)
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        status = (payload.get("status") or "").strip() or None
        severity = (payload.get("severity") or "").strip() or None
        artifact_type = (payload.get("artifact_type") or "").strip() or None
        limit = _as_int(payload, "limit", 100, max_value=1000)
        offset = _as_int(payload, "offset", 0, max_value=100000)
        rows = self.overseer_db.list_dialectics(
            status=status, severity=severity,
            artifact_type=artifact_type,
            limit=limit, offset=offset,
        )
        # Trim long text fields in list view; full text via /dialectic/get
        for r in rows:
            for k in ("opus_text", "gemma_text", "source_context"):
                v = r.get(k) or ""
                if len(v) > 280:
                    r[k] = v[:280] + " …"
        return {
            "ok": True,
            "dialectics": rows,
            "counts": self.overseer_db.dialectic_counts(),
        }

    def _http_get_dialectic(self, payload):
        """GET /plugins/overseer/dialectic/get?id=N — full text both sides."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        nid = payload.get("id")
        if nid is None:
            return {"ok": False, "error": "missing 'id'"}
        try:
            row = self.overseer_db.get_dialectic(int(nid))
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be an integer"}
        if not row:
            return {"ok": False, "error": "no such dialectic"}
        return {"ok": True, "dialectic": row}

    def _http_resolve_dialectic(self, payload):
        """POST /plugins/overseer/dialectic/resolve

        Body: {"id": N, "resolution": "opus"|"gemma"|"third"|"productive",
               "resolution_text": "..." (only required for 'third')}
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        nid = payload.get("id")
        resolution = (payload.get("resolution") or "").strip().lower()
        text = (payload.get("resolution_text") or "").strip()
        if nid is None or resolution not in (
                "opus", "gemma", "third", "productive"):
            return {"ok": False,
                    "error": "id and resolution required; resolution must be "
                             "opus|gemma|third|productive"}
        if resolution == "third" and not text:
            return {"ok": False,
                    "error": "resolution='third' requires resolution_text"}
        try:
            ok = self.overseer_db.resolve_dialectic(
                int(nid), resolution=resolution,
                resolution_text=text, resolved_by="user")
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not ok:
            return {"ok": False,
                    "error": "no open dialectic with that id"}

        # 3i CP2: log a correction against the LOSING model so the
        # distill pass can learn from the user's call. 'productive'
        # means "valuable disagreement, keep both" — no error to log.
        # 'third' means user proposed something neither model said —
        # log against both.
        correction_ids = []
        try:
            d = self.overseer_db.get_dialectic(int(nid))
            if d:
                if resolution in ("opus", "gemma"):
                    losing = "gemma" if resolution == "opus" else "opus"
                    cid = self.overseer_db.log_correction(
                        model=d.get(losing + "_model") or "",
                        artifact_table="dialectic_open",
                        artifact_id=int(nid),
                        topic=(d.get("source_context") or "")[:120],
                        what_was_wrong=(d.get(losing + "_text") or "")[:1000],
                        user_correction="user picked {} over {}".format(
                            resolution, losing),
                        severity="med",
                        source="dialectic-resolution",
                    )
                    correction_ids.append(cid)
                elif resolution == "third":
                    for losing in ("opus", "gemma"):
                        cid = self.overseer_db.log_correction(
                            model=d.get(losing + "_model") or "",
                            artifact_table="dialectic_open",
                            artifact_id=int(nid),
                            topic=(d.get("source_context") or "")[:120],
                            what_was_wrong=(d.get(losing + "_text") or "")[:1000],
                            user_correction=text[:2000],
                            severity="med",
                            source="dialectic-resolution",
                        )
                        correction_ids.append(cid)
        except Exception as e:
            log.exception("dialectic correction logging failed: %s", e)

        return {
            "ok": True,
            "dialectic": self.overseer_db.get_dialectic(int(nid)),
            "correction_ids": correction_ids,
        }

    def _http_dialectic_counts(self, payload):
        """GET /plugins/overseer/dialectic/counts"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        return {"ok": True, "counts": self.overseer_db.dialectic_counts()}

    def _http_journal_reflect_now(self, payload):
        """POST /plugins/overseer/journal/reflect-now

        Manual consolidation trigger. Bypasses the is_tick_notable gate
        and asks the overseer to write a reflection right now, drawing
        on whatever's in the most recent tick summary plus current
        working memory. Useful when you want a checkpoint reflection
        that's not tied to a fresh tick.

        Body (all optional): {"force_notable": true}
        """
        if (self.overseer_db is None or self.llm is None
                or self.loop is None):
            return {"ok": False, "error": "overseer not initialized"}
        from journal import write_tick_journal_entry
        from loop import DailyBudget, TickBudget
        # Build a one-call budget so this respects daily caps
        daily = DailyBudget(
            db=self.overseer_db,
            max_cost_usd=float(self.api.config.get(
                "loop_daily_budget_usd", 1.00)),
            max_calls=int(self.api.config.get(
                "loop_daily_budget_calls", 25)),
        )
        budget = TickBudget(
            max_calls=2, max_cost_usd=0.10, daily_budget=daily)
        # Synthesize a notable tick summary so the writer doesn't gate-out
        last = self.loop.stats().get("last_tick_summary") or {}
        synthetic = dict(last) if last else {}
        synthetic["trigger"] = "manual-reflect-now"
        synthetic["manual_reflection"] = True
        wm_json = self.overseer_db.get_overseer_state(
            "working_memory_json")
        wm = None
        if wm_json:
            try:
                wm = json.loads(wm_json)
            except Exception:
                pass
        try:
            jid = write_tick_journal_entry(
                db=self.overseer_db, llm=self.llm,
                tick_summary={**synthetic,
                              "sessions_summarized":
                                  synthetic.get("sessions_summarized", 1)},
                working_memory=wm,
                budget=budget,
                instance_id="manual-reflect@overseer",
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not jid:
            return {"ok": False,
                    "error": "writer returned no entry "
                             "(LLM failure or budget hit)"}
        entry = self.overseer_db.recent_journal_entries(limit=1)[-1]
        return {"ok": True, "entry": entry}

    def _http_journal(self, payload):
        """GET /plugins/overseer/journal?limit=N

        Returns recent overseer journal entries (the thinking layer).
        Append-only by design — no POST/DELETE on this resource."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 30, max_value=500)
        return {
            "ok": True,
            "entries": self.overseer_db.recent_journal_entries(limit),
            "total": self.overseer_db.journal_count(),
        }

    def _http_budget(self, payload):
        """GET /plugins/overseer/budget — today's daily budget snapshot."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        from loop import DailyBudget
        daily = DailyBudget(
            db=self.overseer_db,
            max_cost_usd=float(self.api.config.get(
                "loop_daily_budget_usd", 1.00)),
            max_calls=int(self.api.config.get(
                "loop_daily_budget_calls", 25)),
        )
        return {"ok": True, "budget": daily.snapshot()}

    def _http_budget_override(self, payload):
        """POST /plugins/overseer/budget/override — set or clear the
        manual daily-cap override (Slice 14.7.2, 2026-05-26).

        body:
          {cost_usd: float, calls: int}  — set override values
          {cost_usd: null}               — clear cost override (calls
                                           field optional, same idea)
          {clear: true}                  — clear both overrides

        Override auto-expires at the next local-midnight rollover so
        a forgotten bump doesn't quietly raise tomorrow's ceiling.
        Returns the resulting budget snapshot.

        Use case: bulk-backfill work (drain a large import queue,
        regenerate temporal narratives) that needs more than the
        plugin.toml ceiling for a few hours, without editing config
        or restarting the service.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        from loop import DailyBudget
        payload = payload or {}

        # Branch 1: explicit clear.
        if payload.get("clear"):
            self.overseer_db.delete_overseer_state(
                DailyBudget.KEY_OVERRIDE_COST)
            self.overseer_db.delete_overseer_state(
                DailyBudget.KEY_OVERRIDE_CALLS)
        else:
            # Branch 2: per-field. null clears just that field; a
            # numeric value sets it. Missing field = leave alone.
            if "cost_usd" in payload:
                v = payload["cost_usd"]
                if v is None:
                    self.overseer_db.delete_overseer_state(
                        DailyBudget.KEY_OVERRIDE_COST)
                else:
                    try:
                        cost = float(v)
                        if cost < 0:
                            return {"ok": False,
                                    "error": "cost_usd must be >= 0"}
                        self.overseer_db.set_overseer_state(
                            DailyBudget.KEY_OVERRIDE_COST,
                            round(cost, 4))
                    except (TypeError, ValueError):
                        return {"ok": False,
                                "error": "cost_usd must be a number"}
            if "calls" in payload:
                v = payload["calls"]
                if v is None:
                    self.overseer_db.delete_overseer_state(
                        DailyBudget.KEY_OVERRIDE_CALLS)
                else:
                    try:
                        n = int(float(v))
                        if n < 0:
                            return {"ok": False,
                                    "error": "calls must be >= 0"}
                        self.overseer_db.set_overseer_state(
                            DailyBudget.KEY_OVERRIDE_CALLS, n)
                    except (TypeError, ValueError):
                        return {"ok": False,
                                "error": "calls must be an integer"}

        # Return fresh snapshot reflecting the new cap.
        daily = DailyBudget(
            db=self.overseer_db,
            max_cost_usd=float(self.api.config.get(
                "loop_daily_budget_usd", 1.00)),
            max_calls=int(self.api.config.get(
                "loop_daily_budget_calls", 25)),
        )
        return {"ok": True, "budget": daily.snapshot()}


def register(api):
    """Entry point invoked by plugins_runtime._load_plugin()."""
    return OverseerPlugin(api)
