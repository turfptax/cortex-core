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
from claude_jsonl import (
    CLAUDE_CODE_SOURCE,
    canonicalize_project_name,
    claude_code_imported_id,
    claude_code_session_id_from_path,
    file_sha256,
    parse_claude_code_jsonl,
)
from chat import respond_to_message
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
    return row


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
            Route("GET",  "/chat/history",          self._http_chat_history),
            Route("POST", "/chat/clear",            self._http_chat_clear),
            Route("POST", "/chat/compress",         self._http_chat_compress),
            # ── Slice 3e: notifications ─────────────────────────
            Route("GET",  "/notifications",         self._http_notifications),
            Route("POST", "/notifications/dismiss", self._http_notifications_dismiss),
            Route("POST", "/notifications/action",  self._http_notifications_action),
            Route("POST", "/notifications/respond", self._http_notifications_respond),
            # ── Slice 3e: budget visibility ─────────────────────
            Route("GET",  "/budget",                self._http_budget),
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
        ]

    # ── Lifecycle ───────────────────────────────────────────────

    def on_load(self) -> None:
        # Step 1: open OverseerDB FIRST so the overseer schema exists on
        # the plugin's DB before anything else touches it. Same pattern
        # as the pet plugin (proven by 2c2d).
        overseer_db_path = self.api.plugin_data / "overseer.db"
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
        plugin_folder = self.api.plugin_data.parent
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
                    "loop not started (loop_enabled=false in config)")
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

    def _http_detail(self, payload):
        """GET /plugins/overseer/detail?token=<prefix>:<id>

        Resolve a working_memory token to its full row + tags +
        type-specific context + suggested next-step tokens. Two depths:
        the working_memory artifact gives you breadth, this gives you
        focused depth on one cell of it.
        """
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        token = str(payload.get("token", "")).strip()
        if not token:
            return {"ok": False, "error": "token query param is required"}
        try:
            return resolve_detail(self.overseer_db, token)
        except TokenError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("detail resolution failed for %r", token)
            return {"ok": False, "error": "detail failed: " + str(e)}

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

        # Period bounds — same per-kind dispatch as the loop.
        if kind == "daily":
            period_start, period_end, period_label = (
                temporal_clock.today_local_bounds(local_now))
        elif kind == "weekly":
            period_start, period_end, period_label = (
                temporal_clock.week_local_bounds(local_now))
        elif kind == "monthly":
            period_start, period_end, period_label = (
                temporal_clock.previous_month_local_bounds(local_now))
        else:  # yearly
            period_start, period_end, period_label = (
                temporal_clock.previous_year_local_bounds(local_now))

        # Allow caller to override period_label (re-generate an
        # arbitrary historical period for testing).
        override_label = (payload.get("period_label") or "").strip()
        if override_label:
            period_label = override_label

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
        Body: {"text": "...", "entry_type": "free" (default)}"""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        text = (payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text required"}
        entry_type = (payload.get("entry_type") or "free").strip()
        if entry_type not in ("free", "daily", "weekly"):
            entry_type = "free"
        try:
            new_id = self.overseer_db.add_human_journal_entry(
                text=text,
                entry_type=entry_type,
                local_created_at=temporal_clock.format_local_iso(),
            )
            return {"ok": True, "id": new_id}
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

    def _http_delete_person(self, payload):
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        person_id = payload.get("id")
        if person_id is None:
            return {"ok": False, "error": "id required"}
        n = self.overseer_db.delete_person(int(person_id))
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
            )
        except Exception as e:
            self.api.log.exception("chat failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _http_chat_history(self, payload):
        """GET /plugins/overseer/chat/history?limit=N

        Slice 8: each message dict carries an `attachments` list (empty
        if no files were attached) so the frontend can re-render
        thumbnails after a reload."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        limit = _as_int(payload, "limit", 50, max_value=500)
        return {
            "ok": True,
            "messages": self.overseer_db.recent_chat_messages(limit),
            "total": self.overseer_db.chat_message_count(),
        }

    def _http_chat_clear(self, payload):
        """POST /plugins/overseer/chat/clear — wipe the chat thread.
        Per locked design, append-only is for future_overseer_notes;
        chat is a working thread the user can reset."""
        if self.overseer_db is None:
            return {"ok": False, "error": "overseer not initialized"}
        n = self.overseer_db.chat_message_count()
        self.overseer_db.clear_chat()
        return {"ok": True, "cleared": n}

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
            )
        except Exception as e:
            log.exception("chat/compress failed")
            return {"ok": False, "error": str(e)[:500]}

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


def register(api):
    """Entry point invoked by plugins_runtime._load_plugin()."""
    return OverseerPlugin(api)
