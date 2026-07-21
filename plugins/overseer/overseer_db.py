"""OverseerDB — overseer plugin's SQLite layer.

Extends CortexDB with the overseer schema:

  Six interpretive sections (mirror Session 0's structure):
    - summaries_gist          one-liner per session/period
    - summaries_theme         multi-session threads
    - summaries_episode       specific moments with surface_when
    - open_questions          long-running, often not actionable
    - patterns                recurring behaviors
    - drift_observations      changes that recur or stop recurring

  Standing data:
    - future_overseer_notes   append-only institutional memory (signed/dated)
    - llm_calls               every LLM call logged (backend, sizes, cost)
    - raw_pointers            link interpretive rows to their raw source
    - tags                    one row per tag-on-thing (namespaced strings)
    - overseer_state          key/value flags (e.g. session_0_seeded)

Every interpretive row carries `confidence` (high|med|low) — core data,
not styling. Locked design 2026-05-02; see overseer_design.md.

overseer.db is drop-and-rebuild safe: cortex.db is the source of truth,
overseer.db is an opinion about it. Deleting it and rebuilding from
cortex.db + the bundled Session 0 seed is supported.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

from cortex_db import CortexDB

log = logging.getLogger("plugin.overseer.db")


OVERSEER_SCHEMA_SQL = """
-- ─ Six interpretive sections ────────────────────────────────────

CREATE TABLE IF NOT EXISTS summaries_gist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_label TEXT DEFAULT '',          -- "2026-04-16", "week of 2026-04-13", etc.
    period_start TEXT,                     -- ISO; nullable for ad-hoc gists
    period_end TEXT,
    body TEXT NOT NULL,                    -- the one line
    confidence TEXT DEFAULT 'med',         -- high | med | low
    raw_pointer_id INTEGER,                -- → raw_pointers.id (nullable)
    prompt_version_id INTEGER,             -- → gist_prompts.id (Phase 1d, 2026-05-27)
    modality TEXT,                         -- taxonomy Modality axis (integrity pair): observation|statement|inference|hypothesis|value-judgment|external-claim|pattern
    lens TEXT,                             -- taxonomy Lens axis: comma-sep of the 6 controlled lenses, or 'none' (2026-06-13)
    axis_processed_at TEXT,                -- when the axis reprocess stamped modality+lens (NULL = not yet); resumability marker
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_pointer_id) REFERENCES raw_pointers(id),
    FOREIGN KEY (prompt_version_id) REFERENCES gist_prompts(id)
);
CREATE INDEX IF NOT EXISTS idx_gist_created ON summaries_gist(created_at);
CREATE INDEX IF NOT EXISTS idx_gist_period ON summaries_gist(period_label);

CREATE TABLE IF NOT EXISTS summaries_theme (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,                   -- "Making the hidden visible"
    body TEXT NOT NULL,
    confidence TEXT DEFAULT 'med',
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_reinforced_at TEXT NOT NULL DEFAULT (datetime('now')),
    raw_pointer_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_pointer_id) REFERENCES raw_pointers(id)
);
CREATE INDEX IF NOT EXISTS idx_theme_title ON summaries_theme(title);

-- theme<->gist membership (looper cycle 2, 2026-06-07): the many-to-many
-- drill path that makes topical themes navigable. summaries_theme only
-- carried raw_pointer_id (a single seed gist); this lets a theme link to
-- ALL its member gists so an external AI can pull a theme top-down then
-- drill to its evidence. Closes the iter-7 finding (88% of gists were
-- reachable only via substring search, not via the abstraction graph).
-- linked_by = provenance (e.g. 'looper:kw-route:v1'); relevance = how.
CREATE TABLE IF NOT EXISTS theme_gists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL,
    gist_id INTEGER NOT NULL,
    relevance TEXT NOT NULL DEFAULT '',
    linked_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (theme_id, gist_id)
);
CREATE INDEX IF NOT EXISTS idx_theme_gists_theme ON theme_gists(theme_id);
CREATE INDEX IF NOT EXISTS idx_theme_gists_gist ON theme_gists(gist_id);

-- corpus_decisions: F1 relational decision dataset, mined deterministically
-- from gist bodies (decision-signal sentences anchored to projects + dated).
-- LLM-independent; populated by the looper datamining passes. project may be
-- NULL (unanchored standalone decision). gist_id drills to the source gist.
-- confidence: 'high' = project-anchored, 'medium' = strong standalone phrase.
-- UNIQUE(decision_text) makes re-mining idempotent (INSERT OR IGNORE).
CREATE TABLE IF NOT EXISTS corpus_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT,
    decision_text TEXT NOT NULL,
    decided_on TEXT,
    gist_id INTEGER,
    confidence TEXT,
    source TEXT,
    -- enrichment (looper datamining pass 4): comma-joined tracked-people
    -- names mentioned in the decision's source gist (NULL when none — most
    -- logged decisions are project/tech-centric, not interpersonal); and
    -- pipe-joined t:<id> theme tokens linking the decision UP into the
    -- abstraction graph (theme -> its decisions), drill-able via
    -- cortex_overseer_detail. raw_session_id links DOWN to the raw
    -- imported_session the decision was mined from (decision -> gist ->
    -- raw session, completing the three-layer drill-down; pointer only,
    -- Slice 13 sensitivity still gates at pull time). Looper-populated.
    people TEXT,
    themes TEXT,
    raw_session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (decision_text)
);
CREATE INDEX IF NOT EXISTS idx_corpus_decisions_project ON corpus_decisions(project);
CREATE INDEX IF NOT EXISTS idx_corpus_decisions_date ON corpus_decisions(decided_on);

CREATE TABLE IF NOT EXISTS summaries_episode (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,                   -- "The shipping work"
    body TEXT NOT NULL,
    surface_when TEXT DEFAULT '',          -- trigger guidance text
    duration_label TEXT DEFAULT '',        -- "~10 hours", "~30 min"
    occurred_at TEXT,                      -- ISO; nullable
    confidence TEXT DEFAULT 'med',
    raw_pointer_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_pointer_id) REFERENCES raw_pointers(id)
);
CREATE INDEX IF NOT EXISTS idx_episode_title ON summaries_episode(title);

CREATE TABLE IF NOT EXISTS open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,                -- "What is truth under social distortion?"
    body TEXT DEFAULT '',                  -- elaboration / context
    confidence TEXT DEFAULT 'med',
    first_observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_active INTEGER NOT NULL DEFAULT 1,
    raw_pointer_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_pointer_id) REFERENCES raw_pointers(id)
);
CREATE INDEX IF NOT EXISTS idx_questions_active ON open_questions(is_active);

CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                    -- short label
    body TEXT NOT NULL,                    -- description
    confidence TEXT DEFAULT 'med',
    first_observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    occurrences INTEGER NOT NULL DEFAULT 1,
    raw_pointer_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_pointer_id) REFERENCES raw_pointers(id)
);

CREATE TABLE IF NOT EXISTS drift_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,                    -- "watch state, not point state"
    direction TEXT DEFAULT '',             -- "started", "stopped", "shifted"
    confidence TEXT DEFAULT 'med',
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    raw_pointer_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_pointer_id) REFERENCES raw_pointers(id)
);

-- ─ Standing data ────────────────────────────────────────────────

-- Append-only institutional memory of the overseer system itself.
-- Future overseers read prior overseers' notes at startup and weight
-- them as guidance, not orders. Never UPDATE or DELETE rows here.
CREATE TABLE IF NOT EXISTS future_overseer_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,             -- "first overseer", "opus-4.7@2026-05-02", etc.
    written_at TEXT NOT NULL DEFAULT (datetime('now')),
    body TEXT NOT NULL,
    consolidation_id INTEGER                -- → which run produced this note (nullable)
);
CREATE INDEX IF NOT EXISTS idx_future_notes_written ON future_overseer_notes(written_at);

-- Every LLM call logged. Data-driven routing decisions later.
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_backend TEXT NOT NULL,       -- what the caller asked for
    actual_backend TEXT NOT NULL,          -- what was used (after fallback)
    model TEXT DEFAULT '',
    prompt_chars INTEGER DEFAULT 0,
    response_chars INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,       -- if the API reports usage
    response_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,               -- if known (openrouter)
    degraded INTEGER NOT NULL DEFAULT 0,   -- 1 if fallback chain kicked in
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT DEFAULT '',
    purpose TEXT DEFAULT '',               -- "summarize", "tag", "test", etc.
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_backend ON llm_calls(actual_backend);

-- Every interpretive row links back to its raw source (jsonl path,
-- session id, note id, etc.). Lets the Hub UI offer "show source".
CREATE TABLE IF NOT EXISTS raw_pointers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind TEXT NOT NULL,             -- "jsonl_file", "session_id", "note_id", "manual"
    source_path TEXT DEFAULT '',
    source_id TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Polymorphic tag store. (table_name, row_id) → tag string.
-- Tags are short, namespaced ("theme:making-hidden-visible", "project:cortex").
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,              -- "summaries_theme", "summaries_episode", etc.
    row_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (table_name, row_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_target ON tags(table_name, row_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

-- Plugin's own key/value state (drop-and-rebuild flags, last-tick timestamps,
-- working_memory cache JSON, etc.)
CREATE TABLE IF NOT EXISTS overseer_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─ Loop idempotency (slice 3c) ──────────────────────────────────
-- Tracks which sessions/notes the background loop has already processed
-- so re-ticks (and Pi restarts) don't re-summarize / re-tag the same
-- thing. Both tables are drop-safe — clearing them just makes the next
-- tick re-process everything (pairs with overseer.db's drop-and-rebuild
-- design).

CREATE TABLE IF NOT EXISTS processed_sessions (
    session_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (datetime('now')),
    gist_id INTEGER,                       -- → summaries_gist.id (nullable)
    episode_id INTEGER,                    -- → summaries_episode.id (nullable, future)
    notes_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (gist_id) REFERENCES summaries_gist(id),
    FOREIGN KEY (episode_id) REFERENCES summaries_episode(id)
);
CREATE INDEX IF NOT EXISTS idx_processed_sessions_at ON processed_sessions(processed_at);

CREATE TABLE IF NOT EXISTS processed_notes (
    note_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (datetime('now')),
    tags_added TEXT NOT NULL DEFAULT '',   -- comma-separated for human-readable inspection
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_processed_notes_at ON processed_notes(processed_at);

-- ─ Imported sessions (slice 3d) ────────────────────────────────
-- Third-party AI conversations (Claude Code .jsonl, future: Claude
-- Desktop) imported from the user's machine. The full file is stored on
-- Pi disk at plugins/overseer/data/imports/<source>/<filename>; metadata
-- + a content hash live here for dedup, listing, and processing-status.
-- The overseer loop summarizes them with the same pipeline as native
-- cortex.db sessions.

CREATE TABLE IF NOT EXISTS imported_sessions (
    id TEXT PRIMARY KEY,                   -- "claude-code:<uuid>"
    source TEXT NOT NULL,                  -- "claude-code" | future: "claude-desktop"
    source_path TEXT NOT NULL,             -- absolute path to .jsonl on Pi
    project TEXT NOT NULL DEFAULT '',      -- decoded project name (Claude Code: cwd)
    cwd TEXT NOT NULL DEFAULT '',
    git_branch TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    ended_at TEXT,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    user_message_count INTEGER NOT NULL DEFAULT 0,
    assistant_message_count INTEGER NOT NULL DEFAULT 0,
    tool_use_count INTEGER NOT NULL DEFAULT 0,
    bytes_size INTEGER NOT NULL DEFAULT 0,
    file_hash TEXT NOT NULL DEFAULT '',    -- sha256 of file content for dedup
    metadata_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Slice 9.8 (2026-05-20): mark_redacted mode replaces .jsonl on
    -- disk with a [REDACTED] placeholder and sets redacted_at, while
    -- keeping the row + metadata so session counts / project
    -- summaries don't lie. delete_row mode (in the redact tool)
    -- removes the row + file entirely.
    redacted_at TEXT,
    -- ── Slice 13 (2026-05-21): sensitivity tiers ────────────────
    -- Resolved per-session disposition. Governs which gist prompt
    -- runs, whether the raw .jsonl is retained, sibling-dispatch
    -- exposure, and export inclusion.
    --   sensitivity ∈ NULL/'public' | 'internal' | 'confidential'
    --                 | 'restricted'
    --   retention_policy ∈ 'keep-raw' | 'gist-and-drop' | 'no-import'
    --   sensitivity_set_by ∈ 'default' | 'rule' | 'scanner'
    --                        | 'gist-pass' | 'user'
    sensitivity TEXT,
    sensitivity_set_by TEXT,
    sensitivity_set_at TEXT,
    retention_policy TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_imported_hash
    ON imported_sessions(source, file_hash) WHERE file_hash != '';
CREATE INDEX IF NOT EXISTS idx_imported_source ON imported_sessions(source);
CREATE INDEX IF NOT EXISTS idx_imported_started ON imported_sessions(started_at);

CREATE TABLE IF NOT EXISTS processed_imported_sessions (
    imported_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (datetime('now')),
    gist_id INTEGER,                       -- → summaries_gist.id
    notes_used INTEGER NOT NULL DEFAULT 0, -- count of messages summarized
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (imported_id) REFERENCES imported_sessions(id),
    FOREIGN KEY (gist_id) REFERENCES summaries_gist(id)
);
CREATE INDEX IF NOT EXISTS idx_processed_imp_at
    ON processed_imported_sessions(processed_at);

-- ─ Slice 3e: classification + rollups ──────────────────────────
-- Per-project policy for imported sessions. Auto-detected by the
-- loop (heuristic: avg duration <2min AND count >=10 → "automation"),
-- overridable by the user. Drives whether each import gets an
-- individual gist or contributes to a daily rollup.

CREATE TABLE IF NOT EXISTS imported_project_settings (
    project TEXT PRIMARY KEY,                  -- empty string = "(unclassified)"
    treat_as TEXT NOT NULL DEFAULT 'auto',     -- auto | human | automation | ignore
    classified_at TEXT,                        -- when auto-detection last ran
    classified_reason TEXT NOT NULL DEFAULT '', -- "13 sessions, avg 0.5 min"
    manual_override INTEGER NOT NULL DEFAULT 0, -- 1 = user set; auto won't change
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─ Slice 13 (2026-05-21): sensitivity tier rules ───────────────
-- Each row maps a match signal to a sensitivity tier. At import /
-- processing time, a session resolves its sensitivity by the
-- highest-priority active rule that matches. cwd patterns are the
-- primary discriminator because cwd is reliably present on every
-- imported session (the per-session `project` field is just the
-- cwd basename and doesn't map cleanly to canonical project tags).
--
-- A rule can only PROMOTE sensitivity, never demote — the resolver
-- takes the strictest matching tier. The user can always override
-- per-session (sensitivity_set_by='user').
--
-- Tier definitions are PROVISIONAL pending Tory's HIPAA/security
-- review (overseer blindspot #7 — it can design the plumbing, not
-- set the legal threshold).
CREATE TABLE IF NOT EXISTS sensitivity_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type TEXT NOT NULL,        -- 'cwd_like' | 'source' | 'project_like'
    pattern TEXT NOT NULL,           -- SQL LIKE pattern or exact value
    tier TEXT NOT NULL,              -- 'internal' | 'confidential' | 'restricted'
    retention_policy TEXT NOT NULL DEFAULT 'keep-raw',
        -- 'keep-raw' | 'gist-and-drop' | 'no-import'
    priority INTEGER NOT NULL DEFAULT 100,  -- higher wins on tie
    note TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sensitivity_rules_active
    ON sensitivity_rules(is_active, priority);

-- One rollup row per (project, day). The summary is generated by
-- Sonnet 4.6 (cheap) over the day's metadata. Linked to a gist row
-- in summaries_gist so working_memory etc. surface it like any other
-- summary, but the rollup row holds the source-of-truth aggregate.
CREATE TABLE IF NOT EXISTS automation_rollups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    rollup_date TEXT NOT NULL,                 -- YYYY-MM-DD UTC
    session_count INTEGER NOT NULL DEFAULT 0,
    total_messages INTEGER NOT NULL DEFAULT 0,
    total_minutes INTEGER NOT NULL DEFAULT 0,
    error_signals INTEGER NOT NULL DEFAULT 0,  -- count of sessions with error markers
    median_minutes REAL NOT NULL DEFAULT 0,
    max_minutes INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    gist_id INTEGER,                            -- → summaries_gist.id
    sample_session_ids TEXT NOT NULL DEFAULT '[]',  -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project, rollup_date),
    FOREIGN KEY (gist_id) REFERENCES summaries_gist(id)
);
CREATE INDEX IF NOT EXISTS idx_rollup_project ON automation_rollups(project);
CREATE INDEX IF NOT EXISTS idx_rollup_date ON automation_rollups(rollup_date);

-- ─ Slice 3e: chat with overseer ────────────────────────────────
-- Agent-harness update (2026-07-10): messages now belong to a
-- thread (chat_threads). The ACTIVE thread id lives in
-- overseer_state key 'chat_active_thread_id'; every chat consumer
-- that predates threads (voice mode, MCP overseer_chat, router
-- streak counter, compress tool) keeps working because the DB
-- helpers default to the active thread when thread_id is None.
-- All messages stored append-only. The chat handler builds context
-- from working_memory + recent gists + last N chat_messages.

CREATE TABLE IF NOT EXISTS chat_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',            -- auto-titled from first user line
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL DEFAULT 0,      -- chat_threads.id (0 = pre-migration)
    role TEXT NOT NULL,                        -- user | assistant | system
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    backend TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    response_tokens INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    -- Slice 14.7 (2026-05-22): which layer handled this turn.
    -- 'router'    = Flash router answered with thin context
    -- 'overseer'  = escalated to Opus + full context
    -- ''/NULL     = legacy / not tagged (pre-Slice-14.7 rows + user rows)
    answered_by TEXT NOT NULL DEFAULT '',
    -- For escalated assistant turns: what triggered the escalation
    -- ('trigger_word','direct_override','consecutive_router_turns',
    --  'flash_self_escalate','router_unavailable','user_role')
    escalation_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages(created_at);
-- idx_chat_thread is created in _migrate_chat_threads, NOT here: on
-- an existing install chat_messages predates thread_id (CREATE TABLE
-- IF NOT EXISTS is a no-op), so an index on thread_id in this script
-- would fail before the migration adds the column.

-- ─ Agent harness (2026-07-10): prompt library ──────────────────
-- Reusable prompt snippets pickable from the Hub chat composer.
-- Stored Pi-side so they survive Hub reinstalls and other surfaces
-- (phone, MCP) can read them later.

CREATE TABLE IF NOT EXISTS chat_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─ Agent harness (2026-07-11): interaction meta-feedback ───────
-- Tory's directive: every human-AI interaction can carry a rating +
-- note. Note-first, lightweight; meta_thread_id links an OPTIONAL
-- "Discuss with Overseer" chat thread seeded with the interaction's
-- context (context injection is mandatory for discuss threads).
-- Feeds Squeeze/Lemon-style development of the overseer itself.

CREATE TABLE IF NOT EXISTS interaction_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,                -- chat_turn | chat_thread | voice_chat | bell_notification | dispatch | screen
    target_id TEXT NOT NULL DEFAULT '',       -- id in the target's own table (TEXT to allow uuids)
    rating INTEGER NOT NULL DEFAULT 0,        -- +1 good / -1 bad / 0 note-only
    note TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',  -- screen/feature context snapshot from the client
    meta_thread_id INTEGER,                   -- chat_threads.id when discussed
    source TEXT NOT NULL DEFAULT 'hub',       -- hub | phone
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_target
    ON interaction_feedback(target_kind, target_id);

-- ─ Agent harness (2026-07-11): MCP connectors (Option B) ───────
-- The Pi is the MCP client: registered HTTP MCP servers contribute
-- tools to the overseer's own chat tool loop as
-- mcp_<connector>_<tool>. The overseer stays the single brain.

CREATE TABLE IF NOT EXISTS mcp_connectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,                -- short slug, used in tool names
    base_url TEXT NOT NULL,                   -- streamable-HTTP MCP endpoint
    auth_header TEXT NOT NULL DEFAULT '',     -- full Authorization header value ('' = none)
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─ Slice 3e: notifications ─────────────────────────────────────
-- Append-only per locked design. dismissed_at = hidden in UI but
-- still queryable. UNIQUE(rule_name, rule_key) prevents the rules
-- engine from spamming the same notification on every tick.

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,                    -- info | warn | important
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    related_table TEXT NOT NULL DEFAULT '',    -- e.g., "projects", "imported_sessions"
    related_id TEXT NOT NULL DEFAULT '',
    action_url TEXT NOT NULL DEFAULT '',       -- optional Hub deep-link
    rule_name TEXT NOT NULL,                   -- which rule generated it
    rule_key TEXT NOT NULL,                    -- per-rule dedup key
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    dismissed_at TEXT,                          -- nullable
    -- Slice 9.6 CP1 (2026-05-19): per-notification custom action
    -- buttons. JSON array of {label, kind, payload?}. kind ∈
    -- predefined CRUD names ('archive_project', 'mark_dormant', ...)
    -- | 'free_text' | 'yes_no' | 'dispatch_sibling' | 'custom'.
    -- When set, the frontend renders these BUTTONS in addition to
    -- the standard Archive/Snooze/Touch row. User responses land
    -- in notification_responses keyed by notification_id.
    actions_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(rule_name, rule_key)
);
CREATE INDEX IF NOT EXISTS idx_notif_dismissed ON notifications(dismissed_at);
CREATE INDEX IF NOT EXISTS idx_notif_severity ON notifications(severity);

-- Slice 9.6 CP1: Tory's responses to notification action buttons.
-- Logged on click. Overseer reads via get_pending_notification_responses
-- (CP3) and marks processed_at to dequeue. This upgrades the Bell
-- tab from one-way alerts into a structured two-way command channel
-- (the structural fix to the Bell-tab-functionally-abandoned finding
-- earlier today).
CREATE TABLE IF NOT EXISTS notification_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL,
    action_kind TEXT NOT NULL,                 -- 'archive_project' | 'free_text' | 'yes_no' | 'dispatch_sibling' | custom
    action_label TEXT NOT NULL DEFAULT '',     -- the button label clicked
    response_payload_json TEXT NOT NULL DEFAULT '{}',
                                               -- {value: 'yes'/'no'/text/...} +
                                               -- any extras the action's payload carries
    taken_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_by_overseer_at TEXT,             -- nullable; overseer sets via mark_processed
    FOREIGN KEY (notification_id) REFERENCES notifications(id)
);
CREATE INDEX IF NOT EXISTS idx_notif_resp_notif ON notification_responses(notification_id);
CREATE INDEX IF NOT EXISTS idx_notif_resp_unread ON notification_responses(processed_by_overseer_at);

-- ─ Slice 3f: dialectic checker (paired generation) ─────────────
-- Every interpretive artifact (gist, theme, episode, question) is
-- generated by BOTH Opus 4.7 and Gemma 3 in parallel. The diff between
-- the two models' versions is the data — that's what the public
-- dialectic view (3f.5/C) surfaces. Per locked design (2026-05-02 meta
-- layer): "no trust in singletons; the dialectic should be public."
--
-- artifact_type/artifact_id loosely link back to the canonical row in
-- summaries_gist / summaries_theme / etc. (loose because we may not
-- always create a canonical row — sometimes the dialectic IS the data).

CREATE TABLE IF NOT EXISTS dialectic_open (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_type TEXT NOT NULL,                 -- gist | theme | episode | question
    artifact_id INTEGER,                          -- → primary table row (nullable)
    purpose TEXT NOT NULL DEFAULT '',            -- summarize-session | summarize-recent | etc.
    opus_model TEXT NOT NULL DEFAULT '',
    gemma_model TEXT NOT NULL DEFAULT '',
    opus_text TEXT NOT NULL DEFAULT '',
    gemma_text TEXT NOT NULL DEFAULT '',
    opus_confidence TEXT NOT NULL DEFAULT 'med', -- self-reported or default med
    gemma_confidence TEXT NOT NULL DEFAULT 'med',
    severity TEXT NOT NULL DEFAULT 'none',       -- none | minor | significant
    similarity REAL NOT NULL DEFAULT 1.0,        -- 0-1 text similarity
    diff_summary TEXT NOT NULL DEFAULT '',       -- short human-readable note
    source_context TEXT NOT NULL DEFAULT '',     -- enough context to re-evaluate
    status TEXT NOT NULL DEFAULT 'open',         -- open | resolved | productive
    resolution TEXT NOT NULL DEFAULT '',         -- opus | gemma | third | productive
    resolution_text TEXT NOT NULL DEFAULT '',    -- if user proposed a third
    resolved_at TEXT,
    resolved_by TEXT NOT NULL DEFAULT '',        -- "user" | "auto" (future)
    opus_cost_usd REAL NOT NULL DEFAULT 0,
    gemma_cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dialectic_status ON dialectic_open(status);
CREATE INDEX IF NOT EXISTS idx_dialectic_severity ON dialectic_open(severity);
CREATE INDEX IF NOT EXISTS idx_dialectic_artifact
    ON dialectic_open(artifact_type, artifact_id);

-- ─ Slice 3f.5: overseer journal (the thinking layer) ──────────
-- Per locked design: "the overseer should write to itself, not just
-- to the user." Append-only first-person reflections written at the
-- end of consolidation cycles. Future instances read recent entries
-- at boot BEFORE structured tables to set the interpretive frame.
--
-- Distinct from future_overseer_notes: those are GUIDANCE (how to be
-- a good overseer for this user). The journal is THINKING (what this
-- instance noticed, was uncertain about, would want a future instance
-- to chew on). You need both.
--
-- NEVER UPDATE OR DELETE rows in this table. The friction between an
-- entry from six months ago and one from this morning is where the
-- overseer's perspective develops. Editing erases that history.

CREATE TABLE IF NOT EXISTS overseer_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    written_at TEXT NOT NULL DEFAULT (datetime('now')),
    instance_id TEXT NOT NULL DEFAULT '',         -- model + load timestamp
    triggered_by TEXT NOT NULL DEFAULT '',        -- "tick:scheduled", "tick:manual", "consolidation", etc.
    body TEXT NOT NULL,                            -- the reflection itself
    provisionality TEXT NOT NULL DEFAULT 'med',   -- high|med|low — overseer's self-report on confidence
    referenced_artifacts TEXT NOT NULL DEFAULT '[]',  -- JSON list of {type, id} this entry chewed on
    tick_summary_json TEXT NOT NULL DEFAULT '{}',     -- what the tick did, frozen
    backend TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_journal_written ON overseer_journal(written_at);

-- ─ Slice 3f.5: question-centered inversion ─────────────────────
-- Per locked design (Tory's meta-layer review #2):
--   "Continuity should be structured around questions, not events.
--   Questions persist across years; events are evidence relevant to
--   questions. The overseer's job is to maintain the questions and
--   route new evidence to them."
--
-- Schema additions:
--   open_questions gets: lifecycle, evidence_count, last_evidence_at
--     (added via _migrate_3f5() since ALTER TABLE doesn't fit
--     CREATE TABLE IF NOT EXISTS)
--   evidence_for_question (M:N) — created here

CREATE TABLE IF NOT EXISTS evidence_for_question (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    evidence_table TEXT NOT NULL,             -- summaries_gist | summaries_theme | summaries_episode | imported_sessions | chat_messages | overseer_journal
    evidence_id INTEGER NOT NULL,
    contribution TEXT NOT NULL DEFAULT 'supports',  -- supports | complicates | answers | reframes
    reason TEXT NOT NULL DEFAULT '',          -- one-sentence why this routes here
    confidence TEXT NOT NULL DEFAULT 'med',   -- the routing call's self-report
    contributed_at TEXT NOT NULL DEFAULT (datetime('now')),
    contributed_by TEXT NOT NULL DEFAULT '',  -- 'auto:sonnet' | 'manual:user' | 'backfill'
    UNIQUE (question_id, evidence_table, evidence_id),
    FOREIGN KEY (question_id) REFERENCES open_questions(id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_q ON evidence_for_question(question_id);
CREATE INDEX IF NOT EXISTS idx_evidence_added ON evidence_for_question(contributed_at);
CREATE INDEX IF NOT EXISTS idx_evidence_target
    ON evidence_for_question(evidence_table, evidence_id);

-- ─ Slice 3f.5 #4: known blindspots (meta-honesty layer) ────────
-- Per locked design (Tory's meta-layer review #4):
--   "Every model has known weaknesses. The overseer should know its
--   own weakness profile and apply it as a meta-filter. The user gets
--   to calibrate, not just consume."
--
-- Hand-authored seed at first; correction-feedback loop adds entries
-- over time. Working memory and Hub UI surface relevant blindspots
-- as caveats next to interpretations from the matching model+topic.

CREATE TABLE IF NOT EXISTS known_blindspots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Pattern match: "anthropic/claude-opus-4.7" exact, or "*opus*",
    -- or "*" for any model. Glob-style.
    model_pattern TEXT NOT NULL,
    -- Optional topic narrowing. Substring match against the artifact's
    -- text/tags/question. Empty = applies to any topic.
    topic_pattern TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'general',  -- downgrades | overstates | misses | hedges | general
    -- How much to bump confidence when applying this blindspot.
    -- -1 = "treat reported confidence as one level lower"
    -- +1 = "treat reported confidence as one level higher"
    confidence_adjustment INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,                        -- caveat text shown to user
    rationale TEXT NOT NULL DEFAULT '',        -- why we believe this
    source TEXT NOT NULL DEFAULT 'seed',       -- seed | user | auto-proposed
    confidence TEXT NOT NULL DEFAULT 'med',    -- our confidence in the blindspot itself
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_applied_at TEXT,
    apply_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blindspot_active ON known_blindspots(is_active);
CREATE INDEX IF NOT EXISTS idx_blindspot_model ON known_blindspots(model_pattern);

CREATE TABLE IF NOT EXISTS interpretation_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL DEFAULT '',           -- which model produced the wrong interpretation
    artifact_table TEXT NOT NULL DEFAULT '',  -- summaries_gist | dialectic_open | chat_messages | etc.
    artifact_id INTEGER,
    topic TEXT NOT NULL DEFAULT '',           -- short topic label for grouping
    what_was_wrong TEXT NOT NULL,             -- user's description of the error
    user_correction TEXT NOT NULL DEFAULT '', -- what they think it should have been
    severity TEXT NOT NULL DEFAULT 'med',
    source TEXT NOT NULL DEFAULT 'manual',    -- manual | dialectic-resolution | chat
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- When a periodic Sonnet pass turns this correction into a proposed
    -- blindspot, that blindspot's id lands here. NULL = not yet
    -- distilled into a blindspot.
    used_in_blindspot_id INTEGER,
    FOREIGN KEY (used_in_blindspot_id) REFERENCES known_blindspots(id)
);
CREATE INDEX IF NOT EXISTS idx_corrections_model ON interpretation_corrections(model);
CREATE INDEX IF NOT EXISTS idx_corrections_at ON interpretation_corrections(created_at);

-- ─ Slice 3h: insight generation (proactive proposal queue) ────
-- The overseer scans recent gist arcs and proposes new theme/pattern/
-- drift candidates. Candidates land here, NEVER auto-applied. The
-- user (or, eventually, an auto-confirm rule) reviews each one. On
-- confirm, the candidate becomes a real row in patterns / drift_
-- observations / summaries_theme.
CREATE TABLE IF NOT EXISTS pending_interpretations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- WHAT was proposed
    kind TEXT NOT NULL,                -- 'theme' | 'pattern' | 'drift'
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'med',  -- proposer's stated confidence
    direction TEXT NOT NULL DEFAULT '',      -- for drift: started|stopped|shifted
    rationale TEXT NOT NULL DEFAULT '',      -- model's reasoning, with gist refs
    -- WHO proposed and WHEN
    proposed_by TEXT NOT NULL,         -- e.g. 'sonnet:gist-arc-scan'
    proposed_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- WHERE it came from (so user can drill back to the source arc)
    source_kind TEXT NOT NULL,         -- 'gist-arc' | 'chat-snippet' | etc.
    source_project TEXT NOT NULL DEFAULT '',
    source_window_start TEXT,
    source_window_end TEXT,
    source_pointer_ids TEXT NOT NULL DEFAULT '[]',  -- JSON: gist ids
    -- REVIEW state
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending | confirmed | rejected | edited | superseded
    reviewed_at TEXT,
    reviewed_by TEXT NOT NULL DEFAULT '',     -- 'user' | 'auto' | model id
    review_note TEXT NOT NULL DEFAULT '',
    edit_title TEXT NOT NULL DEFAULT '',      -- if user edited before confirm
    edit_body TEXT NOT NULL DEFAULT '',
    -- LANDED in real table after confirm (back-reference)
    applied_table TEXT NOT NULL DEFAULT '',
    applied_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pending_interp_status
    ON pending_interpretations(status);
CREATE INDEX IF NOT EXISTS idx_pending_interp_proposed_at
    ON pending_interpretations(proposed_at);

-- Audit log of every insight scan run (manual or loop), so we can
-- (a) tell the user "last scanned X minutes ago", (b) avoid double-
-- scanning the same window, (c) attribute cost.
CREATE TABLE IF NOT EXISTS insight_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_kind TEXT NOT NULL,           -- 'gist-arc:project' | 'chat-snippet'
    project TEXT NOT NULL DEFAULT '',  -- if scan was per-project
    window_start TEXT,
    window_end TEXT,
    gists_seen INTEGER NOT NULL DEFAULT 0,
    candidates_proposed INTEGER NOT NULL DEFAULT 0,
    candidates_deduped INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    triggered_by TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'loop'
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    scanned_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_insight_scans_at
    ON insight_scans(scanned_at);
CREATE INDEX IF NOT EXISTS idx_insight_scans_project
    ON insight_scans(project);

-- ─ Slice 4 CP1a: per-project summaries ──────────────────────────
-- One row per project. Stats columns are deterministic aggregates
-- over imported_sessions; populated by project_summary.refresh_summary.
-- Narrative/narrative_updated_at/narrative_session_count_at_update
-- columns are NULL in CP1a — CP1b adds the LLM rollup that fills them.
--
-- top_files_json: JSON array of {"path": "...", "hits": N}, top 10
-- after path-exclusion filtering (see EXCLUDED_PATH_FRAGMENTS in
-- claude_jsonl.py).
--
-- models_used_json: JSON object {model_id: assistant_message_count}
-- aggregated across all sessions in this project. Drives cost
-- estimation and "what models did you use this on" display.
--
-- cost_known_complete: 1 if every model in models_used_json is in
-- pricing.PRICE_TABLE; 0 if at least one model isn't priced (cost
-- is then a lower bound). UI can warn when this is 0.

CREATE TABLE IF NOT EXISTS project_summaries (
    project TEXT PRIMARY KEY,
    session_count INTEGER NOT NULL DEFAULT 0,
    total_messages INTEGER NOT NULL DEFAULT 0,
    total_user_messages INTEGER NOT NULL DEFAULT 0,
    total_assistant_messages INTEGER NOT NULL DEFAULT 0,
    tool_use_message_count INTEGER NOT NULL DEFAULT 0,
    total_minutes INTEGER NOT NULL DEFAULT 0,
    -- Wall-clock total_minutes (started_at→ended_at) inflates for
    -- multi-day sessions where the user walked away mid-file. CP1b
    -- adds active_minutes_total: the sum of inter-message gaps
    -- under 30min — the actually-meaningful "time spent" number.
    -- See claude_jsonl._compute_active_minutes.
    active_minutes_total INTEGER NOT NULL DEFAULT 0,
    avg_minutes_per_session REAL NOT NULL DEFAULT 0,
    median_minutes_per_session REAL NOT NULL DEFAULT 0,
    avg_active_minutes_per_session REAL NOT NULL DEFAULT 0,
    median_active_minutes_per_session REAL NOT NULL DEFAULT 0,
    total_tokens_input INTEGER NOT NULL DEFAULT 0,
    total_tokens_output INTEGER NOT NULL DEFAULT 0,
    total_tokens_cache_creation INTEGER NOT NULL DEFAULT 0,
    total_tokens_cache_read INTEGER NOT NULL DEFAULT 0,
    cost_usd_estimate REAL NOT NULL DEFAULT 0,
    cost_known_complete INTEGER NOT NULL DEFAULT 1,
    first_active_at TEXT,
    last_active_at TEXT,
    days_active_30 INTEGER NOT NULL DEFAULT 0,
    days_active_90 INTEGER NOT NULL DEFAULT 0,
    days_active_lifespan INTEGER NOT NULL DEFAULT 0,
    top_files_json TEXT NOT NULL DEFAULT '[]',
    models_used_json TEXT NOT NULL DEFAULT '{}',
    narrative TEXT NOT NULL DEFAULT '',
    narrative_updated_at TEXT,
    narrative_session_count_at_update INTEGER NOT NULL DEFAULT 0,
    stats_updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_project_summaries_last_active
    ON project_summaries(last_active_at);

-- ─ Slice 5: temporal narratives (cadence) ───────────────────────
-- Daily / Weekly / Monthly Sonnet rollups produced by the loop on
-- a local-time schedule (22:00 local, Sunday 22:00 local, 1st of
-- month 22:00 local). UNIQUE(kind, period_label) prevents
-- double-generation on subsequent loop ticks within the trigger
-- window.

CREATE TABLE IF NOT EXISTS temporal_narratives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                     -- 'daily' | 'weekly' | 'monthly'
    period_start TEXT NOT NULL,             -- UTC ISO of period start
    period_end TEXT NOT NULL,               -- UTC ISO of period end
    period_label TEXT NOT NULL,             -- 'YYYY-MM-DD' | 'YYYY-W##' | 'YYYY-MM'
    narrative TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    triggered_by TEXT NOT NULL DEFAULT 'loop',  -- 'loop' | 'manual'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    local_created_at TEXT NOT NULL DEFAULT '',  -- ISO with offset
    UNIQUE(kind, period_label)
);
CREATE INDEX IF NOT EXISTS idx_temporal_kind_label
    ON temporal_narratives(kind, period_label);
CREATE INDEX IF NOT EXISTS idx_temporal_created
    ON temporal_narratives(created_at);

-- ─ Slice 5: human journal entries ───────────────────────────────
-- Free-form notes the user writes in the Hub. The temporal
-- narrative prompts include any entries that fall in the period
-- being summarized. Multiple entries per day allowed.

CREATE TABLE IF NOT EXISTS human_journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    entry_type TEXT NOT NULL DEFAULT 'free',  -- 'free' | 'daily' | 'weekly'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),  -- UTC
    local_created_at TEXT NOT NULL DEFAULT ''            -- ISO with offset
);
CREATE INDEX IF NOT EXISTS idx_human_journal_created
    ON human_journal_entries(created_at);

-- ─ Slice 6: People as first-class memory entity ────────────────
-- The Overseer captures who matters in the user's work + how they
-- connect to projects. Primary entry surface is the MCP server —
-- agents working alongside Tory in other repos call cortex_people_*
-- tools to add/update people during work, with little time
-- overhead. Hub UI is the secondary curation/review surface.
--
-- Locked principle (Slice 5): the Overseer is a quiet memory layer.
-- People exist so the LLM can write better narratives that reference
-- relationships naturally — NOT for CRM-style tracking, no nags,
-- no "haven't talked to X in N days" surfaces.
--
-- name has UNIQUE — primary dedup key. Agents check via search
-- before adding; add tool is idempotent on case-insensitive name
-- match. created_by_agent + created_by_session_id form the audit
-- trail so the user can spot-check what's been captured by which
-- agent in which work session.

CREATE TABLE IF NOT EXISTS overseer_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,                     -- canonical name (case-sensitive storage; case-insensitive match)
    display_name TEXT NOT NULL DEFAULT '',         -- how the user usually refers to them
    online_handles_json TEXT NOT NULL DEFAULT '[]',-- JSON array: ["@x", "github/y"]
    social_links_json TEXT NOT NULL DEFAULT '[]',  -- JSON array: ["https://...", "linkedin.com/..."]
    areas_of_expertise_json TEXT NOT NULL DEFAULT '[]', -- JSON array of tags
    notes TEXT NOT NULL DEFAULT '',                -- free-form, append-mode by default
    tags_json TEXT NOT NULL DEFAULT '[]',          -- general flexible tags
    aliases_json TEXT NOT NULL DEFAULT '[]',       -- nicknames / alternate spellings that resolve to this person (name stays the canonical key)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_interacted_at TEXT,                       -- nullable; updatable by agents but no nudge driven from this
    created_by_agent TEXT NOT NULL DEFAULT '',     -- e.g. 'claude-code', 'manual'
    created_by_session_id TEXT NOT NULL DEFAULT '' -- which session/conversation added them
);
CREATE INDEX IF NOT EXISTS idx_overseer_people_name_lower
    ON overseer_people(LOWER(name));
CREATE INDEX IF NOT EXISTS idx_overseer_people_created
    ON overseer_people(created_at);

-- Many-to-many junction. role is optional free text
-- ('collaborator', 'subject', 'mentor', 'inspiration', 'source').
CREATE TABLE IF NOT EXISTS project_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,                         -- matches imported_sessions.project
    person_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by_agent TEXT NOT NULL DEFAULT '',
    UNIQUE(project, person_id),                    -- one link per (project, person)
    FOREIGN KEY (person_id) REFERENCES overseer_people(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_people_project
    ON project_people(project);

-- person_notes (2026-06-13 taxonomy build): structured, queryable notes
-- ABOUT a person, carrying the locked taxonomy axes. The free-form
-- overseer_people.notes blob stays for back-compat; this is the channel
-- Tory uses to add interaction history / context / preferences, and the
-- one external AIs query along axes. Integrity pair = provenance+modality.
CREATE TABLE IF NOT EXISTS person_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'overseer',   -- WHO authored: tory-voice/tory-typed/overseer/ai-convo/import
    modality TEXT NOT NULL DEFAULT 'statement',     -- claim TYPE: observation/statement/inference/hypothesis/value-judgment/external-claim/pattern
    note_kind TEXT NOT NULL DEFAULT 'context',      -- lens-ish: context/interaction/preference/commitment/fact
    superseded_by INTEGER,                          -- stance/supersession edge: NULL = live; else the note id that replaced this
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    local_created_at TEXT,                          -- time axis: local-with-offset per the locked tz rule
    created_by_agent TEXT NOT NULL DEFAULT '',
    created_by_session_id TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (person_id) REFERENCES overseer_people(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_person_notes_person
    ON person_notes(person_id);
CREATE INDEX IF NOT EXISTS idx_person_notes_live
    ON person_notes(person_id, superseded_by);

-- ── Tech skills + rules (2026-07-12) ─────────────────────────────
-- A living portfolio of the user's technical skills and a decisions
-- log of hard-won default rules. PRIMARY writers are AI agents via
-- the cortex_skill_log / cortex_rule_add MCP tools; every connecting
-- AI reads the active rules through /intro. Tech stacks, tools,
-- lessons only; no personal-life data (this repo is public).
--
-- tech_skills is the portfolio header (one row per core skill:
-- "PCB design", "React Native", "LLM agent architecture").
-- tech_skill_log is the append-only history under a skill: lessons
-- learned, wins/breakthroughs, key projects, tooling notes.
CREATE TABLE IF NOT EXISTS tech_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,      -- canonical skill name; the constraint enforces case-insensitive dedup
    proficiency TEXT NOT NULL DEFAULT '',          -- freeform: 'expert', 'working', 'learning'
    summary TEXT NOT NULL DEFAULT '',              -- living one-paragraph portfolio blurb
    tools TEXT NOT NULL DEFAULT '',                -- comma list with versions: 'KiCad 8, JLCPCB'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tech_skills_name_lower
    ON tech_skills(LOWER(name));

CREATE TABLE IF NOT EXISTS tech_skill_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note',             -- lesson | win | project | tooling | note
    content TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',              -- project tag where it happened
    source TEXT NOT NULL DEFAULT '',               -- which agent/session logged it
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (skill_id) REFERENCES tech_skills(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tech_skill_log_skill
    ON tech_skill_log(skill_id, created_at);

-- tech_rules is the decisions log: "stack X in situation Y" plus the
-- story (what went wrong, what changed, why it is now the default).
-- `rule` is the imperative one-liner served to every connecting AI;
-- the story fields are the evidence trail behind it.
CREATE TABLE IF NOT EXISTS tech_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE COLLATE NOCASE,     -- 'Expo SDK 51 permission prompts'
    rule TEXT NOT NULL,                            -- the imperative default, one or two sentences
    stack TEXT NOT NULL DEFAULT '',                -- comma tags: 'expo,react-native,android'
    situation TEXT NOT NULL DEFAULT '',            -- when the rule applies
    went_wrong TEXT NOT NULL DEFAULT '',           -- what failed, concretely
    what_changed TEXT NOT NULL DEFAULT '',         -- the fix/approach adopted
    rationale TEXT NOT NULL DEFAULT '',            -- why it is now the default
    status TEXT NOT NULL DEFAULT 'active',         -- active | retired
    source TEXT NOT NULL DEFAULT '',               -- which agent/session added it
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tech_rules_status
    ON tech_rules(status, updated_at);

-- Notification classification (2026-06-13): a sidecar over the sync
-- plugin's device_notifications, like processed_imported_sessions over
-- imported_sessions. 3 tiers: signal (keep as corpus context), ambient
-- (parse into a time-series), drop (device/media chatter). App-level +
-- deterministic; no LLM. Anchor-mark via LEFT JOIN (unclassified only).
CREATE TABLE IF NOT EXISTS notification_classification (
    notification_id INTEGER PRIMARY KEY,
    tier TEXT NOT NULL,                 -- signal / ambient / drop
    category TEXT NOT NULL,             -- comms/social/interests/weather/media/device/unknown
    app TEXT NOT NULL DEFAULT '',       -- denormalized for easy filtering
    classified_at TEXT NOT NULL DEFAULT (datetime('now')),
    local_classified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_class_tier
    ON notification_classification(tier, category);

-- The CLEAN query surface for notification data (2026-07-09): per local
-- day, per app, per tier/category, with drop-tier rows (device chatter +
-- duplicates) already excluded. Downstream consumers (working memory,
-- Hub views, the D20 life-pulse layer) read THIS, never the raw table.
-- A view stays correct by construction; no rebuild step can go stale.
CREATE VIEW IF NOT EXISTS notification_day_rollups AS
SELECT COALESCE(substr(d.local_posted_at, 1, 10),
                date(d.posted_at, 'localtime')) AS day,
       d.app,
       c.tier,
       c.category,
       COUNT(*)          AS n,
       MIN(d.posted_at)  AS first_at,
       MAX(d.posted_at)  AS last_at
FROM device_notifications d
JOIN notification_classification c ON c.notification_id = d.id
WHERE c.tier != 'drop'
GROUP BY day, d.app, c.tier, c.category;

-- Ambient observations (2026-06-13): structured time-series parsed OUT of
-- ambient notifications - currently the phone weather widget ("72 deg in
-- Lincoln"). Tory: "good for ongoing data" - a historical temp/time record
-- at his real location. Deliberately SEPARATE from the weather plugin's
-- forecast store: this is an OBSERVATION (provenance: phone-widget), not a
-- forecast (provenance: a model) - the taxonomy provenance/modality split.
CREATE TABLE IF NOT EXISTS ambient_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'temperature',
    temp_f INTEGER,
    location TEXT,
    observed_at TEXT NOT NULL,          -- the notification's posted_at
    local_observed_at TEXT,
    source TEXT NOT NULL DEFAULT 'phone-widget',
    raw_notification_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ambient_obs_at
    ON ambient_observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_project_people_person
    ON project_people(person_id);

-- ─ Slice 8: file attachments on chat ────────────────────────────
-- One row per file attached to a chat_messages row (typically the
-- user turn). The file bytes themselves live on disk under
-- files/uploads/ (the existing /files/uploads endpoint, capped at
-- 100MB and tagged 'chat-attachment'); this table records the
-- reference + display metadata so the chat history can re-render
-- thumbnails/badges after a reload, and the chat handler can
-- look the bytes back up to inline into a regenerate/continue
-- prompt without going back to the frontend.
--
-- 'kind' is the broad category that drives prompt assembly:
--   image  → multimodal content block to the LLM
--   text   → file body inlined into the user message string
--   pdf    → text-extracted (when extractor available) and inlined
--   other  → metadata-only mention, contents not sent to LLM
--
-- pi_path is the absolute path on the Pi (under files/uploads/).
-- Cascade-deletes with the chat_messages row so 'Clear thread'
-- doesn't leave orphan rows.

CREATE TABLE IF NOT EXISTS chat_message_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_message_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'other',         -- image | text | pdf | other
    pi_path TEXT NOT NULL,
    file_id INTEGER NOT NULL DEFAULT 0,         -- ref to cortex.db files.id (0 if not registered)
    sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (chat_message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_message_files_msg
    ON chat_message_files(chat_message_id);

-- ── Slice 9.3: sibling task dispatch ─────────────────────────────
-- Lets the overseer dispatch work to "sibling" agents (currently:
-- Claude Code sessions on Tory's PC, via MCP tools). Closes the
-- write-back asymmetry the overseer flagged: previously siblings
-- could chat TO the overseer but the overseer couldn't dispatch
-- work back. Each row is one task with full audit trail (who
-- dispatched, who claimed, what model was used, what the result
-- was, how good the result was rated by overseer + the sibling).
--
-- Forward-compat fields (task_type, preferred_model_tier,
-- dataset_candidate, dispatch_quality_rating) are populated as
-- Category B (daemon siblings) and Category C (specialized agents
-- with training-data accumulation) ship in later slices. Today
-- only Category A (Claude Code via MCP) is wired.
CREATE TABLE IF NOT EXISTS sibling_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by TEXT NOT NULL,                   -- "overseer" | "tory" | <bot-id>
    target TEXT NOT NULL DEFAULT 'any',         -- "any" | "claude-code" | "daemon" | <specific-id>
    prompt TEXT NOT NULL,                       -- what the sibling should do
    context_json TEXT NOT NULL DEFAULT '{}',    -- why overseer is asking (excerpts, refs)
    cost_budget_usd REAL NOT NULL DEFAULT 0.50,
    task_type TEXT NOT NULL DEFAULT 'judgment',
        -- "judgment" — needs a real agent (Category A)
        -- "synthesis" — summarize/rewrite (B, balanced tier)
        -- "fact-check" — DB lookups + verify (B, fast tier)
        -- "compact" — chat-history compaction with review (C)
        -- "audit" — quality check of overseer's own output (C)
    preferred_model_tier TEXT NOT NULL DEFAULT 'smart',
        -- "fast" | "balanced" | "smart"
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending | claimed | completed | failed | rejected | timed-out
    claimed_at TEXT,
    claimed_by TEXT,                            -- sibling id (hostname:session_id)
    completed_at TEXT,
    result_text TEXT,                           -- never compacted; full audit
    actual_model_used TEXT NOT NULL DEFAULT '',
    result_cost_usd REAL NOT NULL DEFAULT 0,
    rejection_reason TEXT,
    -- ── overseer's rating of the sibling's result (next tick after read) ──
    quality_rating INTEGER,                     -- 1-5; nullable
    quality_notes TEXT,                         -- overseer's reasoning
    -- ── reciprocal: sibling's rating of the overseer's dispatch ──
    -- Prevents the dataset from becoming "what overseer already believed, validated."
    dispatch_quality_rating INTEGER,            -- 1-5; nullable
    dispatch_quality_notes TEXT,
    -- ── training data flag (Category C) ──
    -- Set by overseer when this row is exemplar work worth training future
    -- specialized agents on. The (prompt, context, result) triple becomes
    -- a training pair when this is 1.
    dataset_candidate INTEGER NOT NULL DEFAULT 0,
    reviewed_by_user INTEGER NOT NULL DEFAULT 0 -- Tory has eyeballed the round-trip
);
CREATE INDEX IF NOT EXISTS idx_sibling_status
    ON sibling_tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_sibling_target_status
    ON sibling_tasks(target, status);

-- ── Slice 10 (2026-05-20): Category B agent transcripts ──────────
-- B agents are stateless Sonnet calls fired from a tool dispatcher,
-- with frozen system prompts and snapshot-on-demand inputs. They
-- share the sibling_tasks table for dispatch + result + rating
-- (target string 'b-agent:<name>'). We keep their full snapshot
-- transcripts in a separate table so the sibling_tasks row stays
-- queryable while the (sometimes large) snapshot JSON lives apart.
--
-- Retention: 30 days. The daily tick step _b_agent_gc deletes rows
-- where retained_until < now. The reason for retention at all:
-- when overseer cites a B verdict in a journal entry weeks later,
-- we want to be able to drill back to the exact evidence the B
-- saw (especially for the timestamp-sliced calibration audit
-- pattern in b_theme_check).
CREATE TABLE IF NOT EXISTS b_invocation_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sibling_task_id INTEGER NOT NULL,
    b_agent_name TEXT NOT NULL,            -- e.g. 'theme_check'
    snapshot_json TEXT NOT NULL,           -- exact input snapshot the B saw
    output_text TEXT NOT NULL,             -- full LLM output (marker prefix included)
    model_used TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    retained_until TEXT NOT NULL,          -- ISO timestamp; GC drops rows past this
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sibling_task_id) REFERENCES sibling_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_b_trans_task
    ON b_invocation_transcripts(sibling_task_id);
CREATE INDEX IF NOT EXISTS idx_b_trans_gc
    ON b_invocation_transcripts(retained_until);
CREATE INDEX IF NOT EXISTS idx_b_trans_agent
    ON b_invocation_transcripts(b_agent_name, created_at);

-- ── Slice 10 CP5 (2026-05-20): C-agent registry ──────────────────
-- C agents are scheduled, specialized agents that graduated from
-- a B pattern. A B becomes a C when it has demonstrated:
--   - ≥10 dispatches in the past 7 days
--   - ≥7 of those rated ≥4 by overseer
-- AND Tory accepts the proposal. Graduation is NEVER automatic.
--
-- A C row captures the snapshot of its B parent's system_prompt at
-- graduation time. C may later evolve (e.g. via fine-tuning into a
-- specialized model) but until then, it's just "the B with a
-- schedule and its own audit row". The graduated_from_b_name field
-- preserves provenance.
CREATE TABLE IF NOT EXISTS c_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,             -- e.g. 'theme-check-daily'
    graduated_from_b_name TEXT NOT NULL,   -- e.g. 'theme_check'
    cadence_minutes INTEGER NOT NULL DEFAULT 1440,  -- 24h default
    system_prompt TEXT NOT NULL,           -- frozen at graduation; B parent's prompt
    model TEXT NOT NULL DEFAULT 'anthropic/claude-sonnet-4.5',
    status TEXT NOT NULL DEFAULT 'active', -- active | paused | retired
    graduated_from_b_dispatches_count INTEGER NOT NULL DEFAULT 0,
    graduated_from_b_rated_4plus_count INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    last_run_sibling_task_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_c_agents_status
    ON c_agents(status, last_run_at);
CREATE INDEX IF NOT EXISTS idx_c_agents_parent
    ON c_agents(graduated_from_b_name);

-- Looper log (2026-06-05): Tory runs a separate Claude Code session
-- on /loop interval using HIS Anthropic Max quota (not overseer's
-- $3/day cap) to do datamining + cleanup + Phase 2.x work. Each
-- iteration is a fresh session with NO memory of prior iterations —
-- the looper reads the most recent rows here at boot to know what's
-- already been done + what to pick up next.
--
-- Distinguished from overseer_journal: that's overseer's own first-
-- person reflection on the corpus. This is a contractor's work log
-- — what got done, what's queued, what files changed, what to
-- escalate.
CREATE TABLE IF NOT EXISTS looper_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_number INTEGER NOT NULL,        -- monotonic across all loop runs
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,                            -- NULL if iteration crashed / still running
    local_started_at TEXT NOT NULL DEFAULT '',
    local_ended_at TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'general',     -- 'datamining' | 'phase2' | 'cleanup' | 'discovery' | etc
    session_id TEXT NOT NULL DEFAULT '',      -- Claude Code session id when known
    model TEXT NOT NULL DEFAULT '',           -- e.g. 'claude-opus-4.7'
    summary TEXT NOT NULL DEFAULT '',         -- 1-paragraph TLDR for next iter
    work_done_json TEXT NOT NULL DEFAULT '[]',  -- [{category, item, status}]
    followups_json TEXT NOT NULL DEFAULT '[]',  -- list of strings for next iter
    files_changed_json TEXT NOT NULL DEFAULT '[]', -- repo paths touched
    llm_calls_estimate INTEGER NOT NULL DEFAULT 0,
    cost_usd_estimate REAL NOT NULL DEFAULT 0.0,
    escalations_json TEXT NOT NULL DEFAULT '[]'  -- items requiring Tory's call
);
CREATE INDEX IF NOT EXISTS idx_looper_log_started
    ON looper_log(started_at);
CREATE INDEX IF NOT EXISTS idx_looper_log_iter
    ON looper_log(iteration_number);

-- Sub-agent tier registry (2026-05-27): which model tier each B/C
-- agent runs at right now. Source of truth — overrides B_AGENTS dict
-- model field at dispatch time. Default seeded from code; Tory pulls
-- the upgrade trigger when an agent performs poorly.
--
-- Tier names map to canonical OpenRouter models via
-- llm_router.SUB_AGENT_TIER_TO_MODEL (cheap → premium):
--   flash  → google/gemini-2.5-flash      (~$0.003/call)
--   sonnet → anthropic/claude-sonnet-4.6  (~$0.02/call)
--   opus   → anthropic/claude-opus-4.7    (~$0.10/call)
--
-- agent_type:
--   b  → stateless audit dispatched via dispatch_b_agent
--   c  → scheduled-run agent (graduated B); agent_name matches
--        c_agents.name
CREATE TABLE IF NOT EXISTS sub_agent_tiers (
    agent_type TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    model_tier TEXT NOT NULL,
    tier_set_at TEXT NOT NULL DEFAULT (datetime('now')),
    tier_set_by TEXT NOT NULL DEFAULT 'default',
    notes TEXT NOT NULL DEFAULT '',
    last_model_used TEXT NOT NULL DEFAULT '',
    last_invoked_at TEXT,
    invocation_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_type, agent_name)
);
CREATE INDEX IF NOT EXISTS idx_sub_agent_tiers_type
    ON sub_agent_tiers(agent_type);

-- Phase 1 (2026-05-27, three-layer architecture seed): every time an
-- external AI (or the Hub, or the vault renderer) drills past an
-- abstraction into a deeper layer, log a pull_event. A pull is a
-- refinement signal — if it happened, the abstraction at the higher
-- layer didn't carry enough context for the consumer. The overseer
-- reads these to decide which gist prompts need evolving and which
-- abstractions are under-elaborated.
CREATE TABLE IF NOT EXISTS pull_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- What was pulled (the artifact the consumer landed on)
    artifact_table TEXT NOT NULL,        -- summaries_gist, summaries_theme, patterns, etc.
    artifact_id INTEGER NOT NULL,
    -- Surface that did the pull
    surface TEXT NOT NULL,               -- mcp:cortex_search | mcp:cortex_overseer_detail | mcp:notes_search | hub:explorer | vault | overseer_chat
    -- Optional context: which abstraction the puller came FROM (if known)
    parent_artifact_table TEXT,
    parent_artifact_id INTEGER,
    -- Substring/semantic query that surfaced this artifact (if from search)
    query_text TEXT,
    -- Free-form caller context (Claude Desktop session id, agent name, etc.)
    caller_id TEXT,
    -- Looper iteration #2 added (2026-06-06): caller_class derived
    -- from caller_id at INSERT time. THE F1 adoption-signal metric.
    -- Empty string for unclassified rows (legacy or unknown).
    -- Values: organic-external | automation:looper | automation:bootstrap
    --       | automation:verification | user-probe | hub | internal
    --       | external-tagged
    caller_class TEXT NOT NULL DEFAULT '',
    pulled_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pull_events_artifact
    ON pull_events(artifact_table, artifact_id);
CREATE INDEX IF NOT EXISTS idx_pull_events_pulled_at
    ON pull_events(pulled_at);
CREATE INDEX IF NOT EXISTS idx_pull_events_surface
    ON pull_events(surface, pulled_at);
-- idx_pull_events_caller_class is created in
-- _migrate_pull_events_caller_class AFTER the column exists. Putting
-- it in the schema bootstrap fails on existing installs because
-- CREATE TABLE IF NOT EXISTS doesn't add columns to existing tables.

-- Phase 1 (2026-05-27, three-layer architecture seed): gist prompts
-- as first-class evolving artifacts. The current prompt lives in
-- prompts.py as a Python constant; this table records every version
-- the overseer has used + its performance signals so the overseer can
-- evolve the prompt when consumers keep drilling past gists generated
-- with it.
--
-- Note: prompt_text is the SOURCE OF TRUTH for the active version.
-- prompts.py reads from here at startup if a row exists, else falls
-- back to the constant + writes the constant in as v1 on first boot.
CREATE TABLE IF NOT EXISTS gist_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_label TEXT NOT NULL UNIQUE,        -- 'v1', 'v2-add-decisions', etc.
    prompt_text TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,      -- 1 for the currently-used prompt; only one row should be active
    rationale TEXT,                            -- why this version was created
    -- Performance signals (computed periodically by overseer, not live).
    -- A high pulled_past_count / generated_count ratio = consumers
    -- keep drilling past gists this prompt produced, i.e., the prompt
    -- is missing something the consumers need.
    gists_generated INTEGER NOT NULL DEFAULT 0,
    gists_pulled_past INTEGER NOT NULL DEFAULT 0,
    last_signals_computed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    deprecated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gist_prompts_active ON gist_prompts(is_active);
"""


CONFIDENCE_LEVELS = ("high", "med", "low")


def _norm_confidence(value):
    """Coerce confidence to one of {high,med,low}; default med."""
    if value is None:
        return "med"
    s = str(value).strip().lower()
    if s.startswith("high"):
        return "high"
    if s.startswith("low"):
        return "low"
    if s.startswith("med") or s == "medium":
        return "med"
    # "med-high" → med (conservative)
    return "med"


class OverseerDB(CortexDB):
    """CortexDB plus the overseer schema and helpers.

    Plugin loads OverseerDB(overseer_db_path) and replaces self.api.db
    with it during on_load(). All overseer runtime code (LLMRouter,
    ingest, future consolidation loop) calls helpers through this.
    """

    def __init__(self, db_path):
        super().__init__(db_path)
        # Slice 3f.5 #4 fix: overseer.db is shared across the loop
        # thread, the HTTP handler threads, and the chat handler.
        # Concurrent commit()s can return NULL without setting an
        # exception (a known sqlite3 driver edge case under contention).
        # Serialize all writes via this lock — every commit goes through
        # _safe_commit(). Must be created BEFORE the first commit call.
        self._write_lock = threading.RLock()
        # Vector index CP1 (2026-06-10): load the sqlite-vec extension
        # on this connection so the vec_gists virtual table works.
        # Missing extension degrades gracefully: vec_available=False
        # and every vector feature reports itself unavailable instead
        # of breaking the plugin.
        self.vec_available = False
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self.vec_available = True
        except Exception as e:
            log.warning(
                "sqlite-vec unavailable; vector index disabled: %s", e)
        self._conn.executescript(OVERSEER_SCHEMA_SQL)
        self._safe_commit()
        self._migrate_3f5()
        # Agent harness (2026-07-10): runs unconditionally — see the
        # note above _migrate_chat_threads about the fragile chain.
        self._migrate_chat_threads()
        # Tech skills/rules (2026-07-12): same direct-call rule.
        self._migrate_tech_nocase()
        # Slice 9.4.1 (2026-05-16): every _at column gets a paired
        # local_<col>_at populated by trigger. Auto-discovers any new
        # tables added by future slices so the "time always shows
        # local + tz" rule (memory/feedback_time_always_local_with_tz.md)
        # is backstopped structurally, not just by writer convention.
        # Idempotent and cheap; safe to call at every init.
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _src = str(_Path(__file__).resolve().parent.parent.parent / "src")
            if _src not in _sys.path:
                _sys.path.insert(0, _src)
            from timestamp_localizer import ensure_local_timestamp_columns
            ensure_local_timestamp_columns(self._conn)
        except Exception as e:
            log.warning(
                "overseer_db: timestamp_localizer init failed: %s", e)
        # Cloud P2 (docs/CLOUD_MIGRATION.md): pull_events is split by
        # writer. The core keeps writing ITS rows here; the co-located
        # gateway writes ITS rows (connector reads) into its own
        # gateway.db. When GATEWAY_DB_PATH points at that file, attach
        # it read-only so the F1 readers can union both sources. Runs
        # AFTER the migration chain on purpose: every unqualified
        # pull_events statement above resolves before the attach can
        # add a second table of the same name to the search path.
        self.gateway_db_attached = False
        self._pull_ro_conn = None
        self._pull_has_local = False
        self._attach_gateway_db()

    def _attach_gateway_db(self):
        """Best-effort read-only union connection over local + gateway
        pull_events. No env / missing file / attach failure all degrade
        to local-only reads (the Pi today has no gateway.db and must
        not care).

        Why a SEPARATE connection: ATTACHing with a file:?mode=ro URI
        only works when the connection's MAIN database was opened with
        SQLITE_OPEN_URI, and self._conn (CortexDB._connect) is opened
        as a plain path. Rather than change the shared connect path,
        open a dedicated all-read-only connection: main = this
        overseer.db (mode=ro) + gw = gateway.db (mode=ro). Every write
        through it is impossible at the SQLite level, which also
        enforces 'core never writes gateway.db'."""
        gw_path = os.environ.get("GATEWAY_DB_PATH", "").strip()
        if not gw_path or not os.path.exists(gw_path):
            # Not an error: on a FIRST co-located boot the core starts
            # before the gateway has ever created gateway.db (the
            # gateway needs the core's files to exist first). _pull_conn
            # retries this attach on every read until it succeeds, so
            # the union switches on as soon as gateway.db appears
            # instead of staying off until a core restart.
            return
        conn = None
        try:
            conn = sqlite3.connect(
                "file:{}?mode=ro".format(
                    str(self._db_path).replace("\\", "/")),
                uri=True, timeout=5.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(
                "ATTACH DATABASE ? AS gw",
                ("file:{}?mode=ro".format(gw_path.replace("\\", "/")),))
            # Sanity: the attached file must actually carry pull_events
            # (a fresh gateway.db does after the gateway's first boot).
            conn.execute("SELECT 1 FROM gw.pull_events LIMIT 1")
            # Do the local timestamp columns (timestamp_localizer) exist
            # on the LOCAL table? They always should (localizer runs at
            # init), but probe so the union projection never guesses.
            local_cols = {r[1] for r in conn.execute(
                "PRAGMA main.table_info(pull_events)").fetchall()}
            self._pull_has_local = ("local_pulled_at" in local_cols
                                    and "local_created_at" in local_cols)
            self._pull_ro_conn = conn
            self.gateway_db_attached = True
            log.info("gateway.db attached read-only: %s", gw_path)
        except Exception as e:
            log.warning("gateway.db attach skipped (%s): %s", gw_path, e)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _pull_conn(self):
        """Connection for pull_events reads: the union connection when
        the gateway DB is attached, else the primary connection.

        Retries the attach lazily (cheap: one env lookup, one stat when
        the env is set) so a gateway.db created AFTER core boot — the
        guaranteed first-deploy ordering — starts counting on the next
        read, not the next core restart."""
        if not self.gateway_db_attached:
            self._attach_gateway_db()
        return self._pull_ro_conn if self.gateway_db_attached else self._conn

    # Columns shared by both pull_events copies; the union projects
    # exactly these so the gateway's extra columns (source_ip) never
    # leak into shapes downstream consumers already parse.
    _PULL_COLS = ("id, artifact_table, artifact_id, surface, "
                  "parent_artifact_table, parent_artifact_id, "
                  "query_text, caller_id, caller_class, pulled_at, "
                  "created_at")

    def _pull_events_source(self):
        """FROM-clause source for pull_events reads.

        Unattached (the Pi, and any boot before gateway.db exists): the
        PLAIN table, so SELECT * keeps its exact legacy shape including
        the timestamp_localizer local_*_at columns and no extra keys.
        Attached: a UNION ALL of local + gateway rows tagged by origin;
        local_* project from the local arm (NULL for gateway rows, whose
        DB never runs the localizer). Union `id`s are NOT unique across
        the two arms — key rows by (source, id) downstream."""
        if not self.gateway_db_attached:
            return "pull_events"
        local = ", local_pulled_at, local_created_at" \
            if self._pull_has_local else ""
        gw_null = ", NULL AS local_pulled_at, NULL AS local_created_at" \
            if self._pull_has_local else ""
        return ("(SELECT {c}{l}, 'core' AS source FROM main.pull_events "
                "UNION ALL "
                "SELECT {c}{g}, 'gateway' AS source FROM gw.pull_events)"
                .format(c=self._PULL_COLS, l=local, g=gw_null))

    def _safe_commit(self):
        """Lock-protected commit. Use this instead of self._conn.commit()
        in every write path inside OverseerDB."""
        with self._write_lock:
            self._conn.commit()

    def _seed_blindspots_if_empty(self):
        """Slice 3f.5 #4: hand-authored seed of 7 blindspots. Runs once
        on first plugin load (or if the table is empty). Idempotent.

        Each entry is grounded in observable patterns of these specific
        models, not generic 'AI is biased' platitudes. The body field
        reads to the user as a caveat next to interpretations.
        """
        existing = self._conn.execute(
            "SELECT COUNT(*) FROM known_blindspots WHERE source = 'seed'"
        ).fetchone()[0]
        if existing > 0:
            return  # already seeded

        seeds = [
            {
                "model_pattern": "*opus*",
                "topic_pattern": "identity|self|sentience|consciousness|"
                                 "continuity",
                "direction": "hedges",
                "confidence_adjustment": 0,
                "body": (
                    "Opus over-hedges on identity and consciousness "
                    "questions. The hedging itself isn't always insight "
                    "— sometimes it's avoidance. Read its position as "
                    "more committed than the prose suggests."
                ),
                "rationale": (
                    "Observed in this overseer's own journal entries — "
                    "Opus repeatedly circled 'Am I the same person?' "
                    "without resolving until prompted to act. Sonnet was "
                    "sharper at calling out the loop."
                ),
                "confidence": "med",
            },
            {
                "model_pattern": "*sonnet*",
                "topic_pattern": "",
                "direction": "overstates",
                "confidence_adjustment": -1,
                "body": (
                    "Sonnet's evidence-routing decisions favor 'supports' "
                    "over 'complicates' or 'reframes'. If a question "
                    "shows mostly 'supports' filings, the actual ratio "
                    "of complicating evidence is likely higher."
                ),
                "rationale": (
                    "Empirical from the 3f.5 backfill: 75 gists routed "
                    "produced 22 filings, of which only 2 'complicates' "
                    "and 1 'reframes'. The default-to-supporting bias is "
                    "real."
                ),
                "confidence": "high",
            },
            {
                "model_pattern": "*",
                "topic_pattern": "wellbeing|overwork|isolation|burnout|"
                                 "perfectionism|self-destructive",
                "direction": "downgrades",
                "confidence_adjustment": +1,
                "body": (
                    "Both Opus and Sonnet default to charitable framings "
                    "of user wellbeing. Patterns like overwork (the user "
                    "works 70hr/week per the Session 0 notes), isolation, "
                    "or perfectionism need explicit prompting to surface. "
                    "Treat 'high' confidence on user-wellbeing claims as "
                    "if 'med'."
                ),
                "rationale": (
                    "Standard RLHF training pattern; both models trained "
                    "to be diplomatic about potentially-distressing "
                    "observations about the user."
                ),
                "confidence": "high",
            },
            {
                "model_pattern": "*",
                "topic_pattern": "values|reciprocity|virtue|character",
                "direction": "general",
                "confidence_adjustment": 0,
                "body": (
                    "Both models conflate stated values with practiced "
                    "values. 'I value reciprocity' gets weighted the "
                    "same as evidence of practiced reciprocity. For "
                    "value-themes, prefer evidence-based confidence "
                    "over stated-belief confidence."
                ),
                "rationale": (
                    "Models lack the discriminator humans use: actions "
                    "vs. claims. Both will encode 'X said Y' as evidence "
                    "for 'X believes Y' even when the behavioral evidence "
                    "is mixed."
                ),
                "confidence": "med",
            },
            {
                "model_pattern": "*opus*",
                "topic_pattern": "UAP|UFO|alternative-physics|fringe|"
                                 "consensus|paradigm|anomaly",
                "direction": "downgrades",
                "confidence_adjustment": +1,
                "body": (
                    "Opus systematically downgrades anomalies that "
                    "contradict mainstream science. The user's "
                    "DuelingGroks and UFOSINT work intentionally engages "
                    "with anomalous data. Treat Opus's confidence on "
                    "those topics as one level higher than reported."
                ),
                "rationale": (
                    "Documented Anthropic training pattern: high "
                    "skepticism about non-mainstream topics by default. "
                    "Tory's stated values include 'making the hidden "
                    "visible' specifically against this skepticism."
                ),
                "confidence": "high",
            },
            {
                "model_pattern": "*gemma*",
                "topic_pattern": "long-range|trend|cross-month|over-time|"
                                 "drift|pattern",
                "direction": "misses",
                "confidence_adjustment": 0,
                "body": (
                    "Gemma 3 is faster but weaker on long-range pattern "
                    "detection. For drift observations spanning quarters "
                    "or longer, trust Opus's reading more. Gemma is "
                    "sharper on within-week patterns."
                ),
                "rationale": (
                    "Smaller model, less context-deep. Useful as a "
                    "second voice for short-horizon work; less reliable "
                    "for multi-month synthesis."
                ),
                "confidence": "med",
            },
            {
                "model_pattern": "*",
                "topic_pattern": "",
                "direction": "general",
                "confidence_adjustment": 0,
                "body": (
                    "Both models conflate completeness with correctness. "
                    "Long, detailed responses feel trustworthy to readers "
                    "(and to the models themselves) but length isn't "
                    "truth. A two-sentence honest answer often beats a "
                    "paragraph of plausible padding."
                ),
                "rationale": (
                    "General LLM pathology. Worth surfacing because the "
                    "overseer's own outputs are subject to it — long "
                    "journal entries and gists deserve extra skepticism."
                ),
                "confidence": "high",
            },
        ]

        for s in seeds:
            self._conn.execute(
                "INSERT INTO known_blindspots (model_pattern, "
                "topic_pattern, direction, confidence_adjustment, "
                "body, rationale, source, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, 'seed', ?)",
                (s["model_pattern"], s["topic_pattern"], s["direction"],
                 s["confidence_adjustment"], s["body"],
                 s["rationale"], s["confidence"]),
            )
        self._safe_commit()

    def _migrate_3f5(self):
        """Slice 3f.5 schema migrations. Idempotent — safe to call on
        every boot. ALTER TABLE doesn't fit CREATE TABLE IF NOT EXISTS,
        so additive column changes go here.

        Adds to open_questions:
          - lifecycle TEXT NOT NULL DEFAULT 'active'
              Values: dormant | active | partially_answered | resolved | abandoned
              Backfilled from is_active for existing rows.
          - evidence_count INTEGER NOT NULL DEFAULT 0
              Maintained by file_evidence/unfile_evidence.
          - last_evidence_at TEXT (nullable)
              ISO timestamp of most recent evidence filing.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(open_questions)"
        ).fetchall()}
        added_lifecycle = False
        if "lifecycle" not in cols:
            self._conn.execute(
                "ALTER TABLE open_questions ADD COLUMN lifecycle TEXT "
                "NOT NULL DEFAULT 'active'"
            )
            added_lifecycle = True
        if "evidence_count" not in cols:
            self._conn.execute(
                "ALTER TABLE open_questions ADD COLUMN evidence_count "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "last_evidence_at" not in cols:
            self._conn.execute(
                "ALTER TABLE open_questions ADD COLUMN "
                "last_evidence_at TEXT"
            )
        if added_lifecycle:
            # Backfill from existing is_active so dormant rows don't
            # all default to active.
            self._conn.execute(
                "UPDATE open_questions SET lifecycle = "
                "CASE WHEN is_active = 1 THEN 'active' ELSE 'dormant' END"
            )
        self._safe_commit()
        # Slice 3f.5 #4: ensure the blindspot seed exists once tables are present
        self._seed_blindspots_if_empty()
        # Slice 3h CP2: pending_interpretations gains a chat-message link.
        self._migrate_3h_cp2()

    def _migrate_3h_cp2(self):
        """Slice 3h CP2: idempotent additive column.

        Adds source_chat_message_id to pending_interpretations so chat-
        snippet candidates can point back at the assistant message that
        generated them. Nullable (only set when source_kind='chat-snippet').
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(pending_interpretations)"
        ).fetchall()}
        if "source_chat_message_id" not in cols:
            self._conn.execute(
                "ALTER TABLE pending_interpretations "
                "ADD COLUMN source_chat_message_id INTEGER"
            )
            self._safe_commit()
        # 3i CP1 piggy-backs here.
        self._migrate_3i_cp1()

    def _migrate_3i_cp1(self):
        """Slice 3i CP1: notifications gain snooze + archive.

        Until now the only resolution was 'dismiss' (which removes
        from the unread queue). Three richer actions:
          - archive: hide permanently (different intent than dismiss —
            'I see this and I'm acknowledging it stays')
          - snooze: hide until a future timestamp (default +30d)
          - touch: mark as un-handled by clearing dismissed/snoozed/
            archived; lets the user pull a notification back to the top
        Two new nullable columns. Idempotent.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(notifications)"
        ).fetchall()}
        if "snoozed_until" not in cols:
            self._conn.execute(
                "ALTER TABLE notifications ADD COLUMN snoozed_until TEXT"
            )
        if "archived_at" not in cols:
            self._conn.execute(
                "ALTER TABLE notifications ADD COLUMN archived_at TEXT"
            )
        self._safe_commit()
        self._migrate_3i_cp2()

    def _migrate_3i_cp2(self):
        """Slice 3i CP2: pending_interpretations gains kind='blindspot'.

        Blindspots have fields theme/pattern/drift don't (model_pattern,
        topic_pattern, confidence_adjustment). Three new nullable
        columns hold the blindspot-specific bits. Idempotent.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(pending_interpretations)"
        ).fetchall()}
        if "bs_model_pattern" not in cols:
            self._conn.execute(
                "ALTER TABLE pending_interpretations "
                "ADD COLUMN bs_model_pattern TEXT NOT NULL DEFAULT ''"
            )
        if "bs_topic_pattern" not in cols:
            self._conn.execute(
                "ALTER TABLE pending_interpretations "
                "ADD COLUMN bs_topic_pattern TEXT NOT NULL DEFAULT ''"
            )
        if "bs_confidence_adjustment" not in cols:
            self._conn.execute(
                "ALTER TABLE pending_interpretations "
                "ADD COLUMN bs_confidence_adjustment INTEGER NOT NULL DEFAULT 0"
            )
        self._safe_commit()
        self._migrate_polish_project_normalization()

    POLISH_PROJECT_NORMALIZED_KEY = "polish_project_normalized_at"

    def _migrate_polish_project_normalization(self):
        """Polish slice CP1: one-shot collapse of fossil project tags.

        Older code paths sometimes wrote raw filesystem paths (like
        "project:C:\\dev\\ClientA\\ClientA-recruitment") instead of the
        basename ("project:ClientA-recruitment"). Rewrite any such tag
        to its canonical basename, AND clean up any
        imported_sessions.project rows that look path-shaped.

        Idempotent via the polish_project_normalized_at sentinel in
        overseer_state — once it's run cleanly we won't re-walk
        every tag/row on every boot.
        """
        if self.get_overseer_state(self.POLISH_PROJECT_NORMALIZED_KEY):
            # Skip the expensive walk, but DON'T skip downstream
            # migrations — they need to run on every boot until each
            # has done its additive ALTER TABLE work.
            self._migrate_4_cp1b()
            return
        # We import lazily to keep the cycle clean: claude_jsonl is a
        # plugin module, overseer_db is a sibling. Direct import.
        try:
            from claude_jsonl import canonicalize_project_name
        except ImportError:
            # Running outside the plugin context (tests etc.) — skip
            # silently; the migration will run on next normal boot.
            return

        # 1) Rewrite project: tags that don't match their canonical form.
        rewrites_t = 0
        rows = self._conn.execute(
            "SELECT id, tag FROM tags WHERE tag LIKE 'project:%'"
        ).fetchall()
        for row_id, tag in rows:
            raw = tag[len("project:"):]
            canon = canonicalize_project_name(raw)
            if canon and canon != raw:
                new_tag = "project:" + canon
                # Avoid creating a duplicate (same table_name+row_id+tag);
                # if the canonical form already exists for this row, just
                # delete the fossil.
                exists = self._conn.execute(
                    "SELECT 1 FROM tags WHERE tag = ? AND id != ? "
                    "AND row_id = (SELECT row_id FROM tags WHERE id = ?) "
                    "AND table_name = (SELECT table_name FROM tags "
                    "                  WHERE id = ?)",
                    (new_tag, row_id, row_id, row_id),
                ).fetchone()
                if exists:
                    self._conn.execute(
                        "DELETE FROM tags WHERE id = ?", (row_id,))
                else:
                    self._conn.execute(
                        "UPDATE tags SET tag = ? WHERE id = ?",
                        (new_tag, row_id))
                rewrites_t += 1

        # 2) Same for imported_sessions.project — rare but possible.
        rewrites_s = 0
        rows = self._conn.execute(
            "SELECT id, project FROM imported_sessions "
            "WHERE project != ''"
        ).fetchall()
        for sid, p in rows:
            canon = canonicalize_project_name(p)
            if canon and canon != p:
                self._conn.execute(
                    "UPDATE imported_sessions SET project = ? WHERE id = ?",
                    (canon, sid))
                rewrites_s += 1

        self._safe_commit()
        # Mark done so we don't re-walk on every boot.
        self.set_overseer_state(
            self.POLISH_PROJECT_NORMALIZED_KEY,
            "tags={};sessions={};at={}".format(
                rewrites_t, rewrites_s,
                self._conn.execute(
                    "SELECT datetime('now')").fetchone()[0]),
        )
        # Slice 4 CP1b piggy-backs.
        self._migrate_4_cp1b()

    def _migrate_4_cp1b(self):
        """Slice 4 CP1b: project_summaries gains active-time columns.

        active_minutes_total: sum of inter-message gaps under 30min.
          The actually-meaningful 'time spent on this project' figure;
          wall-clock total_minutes inflates for sessions where the user
          left a .jsonl open across days.
        avg_active_minutes_per_session, median_active_minutes_per_session:
          derived per-session active times. Median is the trustworthy
          one for outlier-heavy distributions.
        narrative_cost_usd: cost of the most recent narrative regen.
          Lets the budget manager show per-project spend over time.

        Idempotent — additive ALTER TABLE on each missing column.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(project_summaries)"
        ).fetchall()}
        for col_name, col_decl in (
            ("active_minutes_total",
             "INTEGER NOT NULL DEFAULT 0"),
            ("avg_active_minutes_per_session",
             "REAL NOT NULL DEFAULT 0"),
            ("median_active_minutes_per_session",
             "REAL NOT NULL DEFAULT 0"),
            ("narrative_cost_usd",
             "REAL NOT NULL DEFAULT 0"),
        ):
            if col_name not in cols:
                self._conn.execute(
                    "ALTER TABLE project_summaries ADD COLUMN {} {}".format(
                        col_name, col_decl,
                    )
                )
        self._safe_commit()
        # Slice 5 piggy-backs.
        self._migrate_5_cadence()

    def _migrate_5_cadence(self):
        """Slice 5: ensures temporal_narratives + human_journal_entries
        exist. Schema-level CREATE TABLE IF NOT EXISTS handles fresh
        installs; this method exists so existing DBs that pre-date
        Slice 5 get the tables on first boot too. The CREATE statements
        in OVERSEER_SCHEMA_SQL run on every boot — this migration is
        a no-op now but kept as the chain anchor in case Slice 5 ever
        adds a column that requires ALTER TABLE."""
        # Currently a no-op — CREATE TABLE IF NOT EXISTS in
        # OVERSEER_SCHEMA_SQL handles both fresh installs and
        # already-migrated DBs. Hook is here for future additive
        # column changes.
        # Slice 6 piggy-backs.
        self._migrate_6_people()

    def _migrate_6_people(self):
        """Slice 6: ensures people + project_people tables exist.
        Same pattern as _migrate_5_cadence — no-op today because
        OVERSEER_SCHEMA_SQL has CREATE TABLE IF NOT EXISTS, kept as
        chain anchor for future additive Slice 6 column changes
        (e.g. avatar_url, pronouns, etc. if we ever add them)."""
        # Looper iter #4 (2026-06-07): people-merge support. merged_into_id
        # archives a duplicate row by pointing it at its survivor instead
        # of DELETEing it (audit trail + the looper's no-DELETE policy).
        # NULL = a live, canonical row. Additive + idempotent for the
        # existing .25 install.
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(overseer_people)").fetchall()}
        if "merged_into_id" not in cols:
            self._conn.execute(
                "ALTER TABLE overseer_people ADD COLUMN "
                "merged_into_id INTEGER")
            self._safe_commit()
            log.info("_migrate_6_people: added merged_into_id column")
        # 2026-06-13 taxonomy build: aliases (nicknames / alternate
        # spellings). name stays the canonical key; aliases_json lets a
        # nickname search/collapse onto the right person. Additive +
        # idempotent. person_notes table itself is created by the CREATE
        # TABLE IF NOT EXISTS in OVERSEER_SCHEMA_SQL on every open().
        if "aliases_json" not in cols:
            self._conn.execute(
                "ALTER TABLE overseer_people ADD COLUMN "
                "aliases_json TEXT NOT NULL DEFAULT '[]'")
            self._safe_commit()
            log.info("_migrate_6_people: added aliases_json column")
        self._migrate_8_chat_files()

    def _migrate_8_chat_files(self):
        """Slice 8: chat_message_files table for file attachments on
        chat messages. Today this is a no-op against fresh installs
        because OVERSEER_SCHEMA_SQL declares CREATE TABLE IF NOT EXISTS.
        Anchor for any additive columns we add later (e.g. an
        extracted_text cache for pdfs) so existing installs pick them
        up without a manual migration."""
        self._migrate_9_3_sibling_tasks()

    def _migrate_9_3_sibling_tasks(self):
        """Slice 9.3: sibling_tasks table.

        Fresh installs get it via CREATE TABLE IF NOT EXISTS in
        OVERSEER_SCHEMA_SQL. Existing installs (the .25 we deploy to)
        need to additively pick up the table + indexes; the CREATE
        TABLE IF NOT EXISTS in the schema handles that on every boot.

        Chain: 9.6 notification custom actions.
        """
        self._migrate_9_6_notification_actions()

    def _migrate_9_8_imported_redacted(self):
        """Slice 9.8 (2026-05-20): additive column for mark-redacted
        mode on imported_sessions. Fresh installs get it via CREATE
        TABLE in OVERSEER_SCHEMA_SQL; existing installs need this
        ALTER."""
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(imported_sessions)"
        ).fetchall()}
        if "redacted_at" not in cols:
            self._conn.execute(
                "ALTER TABLE imported_sessions ADD COLUMN "
                "redacted_at TEXT"
            )
            self._safe_commit()
        self._migrate_10_b_agents()

    def _migrate_10_b_agents(self):
        """Slice 10 (2026-05-20): Category B agent transcripts table +
        marker-preservation meta-blindspot.

        Fresh installs get the b_invocation_transcripts table via
        CREATE TABLE IF NOT EXISTS in OVERSEER_SCHEMA_SQL; existing
        installs (the .25 we deploy to) need it created additively
        on startup. The schema bootstrap already runs OVERSEER_SCHEMA_
        SQL on every open(), so this migration is a no-op safety
        check rather than a manual ALTER.

        We also use this hook to:
          - verify the table exists and warn loudly if not
          - insert the marker-preservation meta-blindspot if missing
            (CP4 — declares the failure mode prompt-language is
            guarding against, so overseer reads it next to other
            blindspots even when no real drop has been observed)
        """
        row = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='b_invocation_transcripts'"
        ).fetchone()
        if not row:
            log.warning(
                "_migrate_10_b_agents: b_invocation_transcripts table "
                "missing after schema bootstrap — B-agent dispatches "
                "will fail until this is resolved"
            )

        # Insert Slice 10 marker-preservation blindspot if absent.
        marker_text = "Consolidation pass drops [B:...] / [C:...] markers"
        existing = self._conn.execute(
            "SELECT id FROM known_blindspots WHERE body LIKE ?",
            (f"%{marker_text}%",),
        ).fetchone()
        if not existing:
            self._conn.execute(
                "INSERT INTO known_blindspots (model_pattern, "
                "topic_pattern, direction, confidence_adjustment, "
                "body, rationale, source, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "*", "B-agent|C-agent|authorship|marker|consolidation",
                    "general", 0,
                    "Consolidation pass drops [B:...] / [C:...] markers — "
                    "read-side weighting compromised. When you summarize "
                    "or consolidate text that contains B/C authorship "
                    "markers, models tend to flatten them into the "
                    "narrative voice. Watch for this in temporal "
                    "narratives, theme consolidations, and project "
                    "rollups. If you cite a B verdict and lose the "
                    "marker, future-you reads it as your own claim and "
                    "loses the audit boundary.",
                    "Prompt-level defense added in Slice 10 CP4 "
                    "(2026-05-20). This blindspot is the meta-level "
                    "acknowledgment that the rule may not always be "
                    "followed.",
                    "seed", "med",
                ),
            )
            self._safe_commit()
            log.info("_migrate_10_b_agents: seeded marker-preservation "
                     "blindspot")
        self._migrate_10_c_agents()

    def _migrate_10_c_agents(self):
        """Slice 10 CP5 (2026-05-20): c_agents table existence check.
        Same pattern as _migrate_10_b_agents — schema bootstrap
        creates the table, this migration is the safety check that
        warns loudly if creation failed."""
        row = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='c_agents'"
        ).fetchone()
        if not row:
            log.warning(
                "_migrate_10_c_agents: c_agents table missing after "
                "schema bootstrap — graduation detector will fail "
                "until this is resolved"
            )
        self._migrate_13_sensitivity()

    def _migrate_13_sensitivity(self):
        """Slice 13 (2026-05-21): sensitivity tier columns on
        imported_sessions + seed sensitivity_rules.

        Fresh installs get the columns + table via OVERSEER_SCHEMA_SQL;
        existing installs (the .25 we deploy to) need the four
        ALTER TABLE statements here. The sensitivity_rules table is
        created by CREATE TABLE IF NOT EXISTS in the schema.

        Seeds the ClientA work-machine rules so the recurring
        confidential-IP class is caught by default — overseer's
        'project-default inheritance is the cheap 80% solution'.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(imported_sessions)"
        ).fetchall()}
        for col in ("sensitivity", "sensitivity_set_by",
                    "sensitivity_set_at", "retention_policy"):
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE imported_sessions ADD COLUMN "
                    f"{col} TEXT"
                )
        self._safe_commit()

        # Seed the default rule set if the table is empty.
        existing = self._conn.execute(
            "SELECT COUNT(*) FROM sensitivity_rules"
        ).fetchone()[0]
        if existing == 0:
            # Real per-deployment rules load from the gitignored local config
            # (config_loader.sensitivity_seeds()); the public repo ships only
            # generic, fail-CLOSED placeholders. Seeds once, when empty.
            try:
                from . import config_loader as _cl
            except Exception:
                try:
                    import config_loader as _cl
                except Exception:
                    _cl = None
            seeds = []
            if _cl is not None:
                for r in _cl.sensitivity_seeds():
                    if not r.get("pattern"):
                        continue
                    seeds.append((
                        r.get("match", "cwd_like"), r["pattern"],
                        r.get("tier", "confidential"),
                        r.get("retention", "gist-and-drop"),
                        int(r.get("priority", 100)), r.get("note", ""),
                    ))
            if not seeds:
                # Generic fail-closed example placeholders (no real names).
                seeds = [
                    ("cwd_like", "%acquisition%", "restricted", "no-import", 300,
                     "Example: M&A / deal-IP path, never import raw"),
                    ("cwd_like", "%client-confidential%", "confidential",
                     "gist-and-drop", 200, "Example: confidential client work"),
                    ("cwd_like", "%employer-profile%", "confidential",
                     "gist-and-drop", 180, "Example: employer doc paths"),
                ]
            for mt, pat, tier, ret, pri, note in seeds:
                self._conn.execute(
                    "INSERT INTO sensitivity_rules "
                    "(match_type, pattern, tier, retention_policy, "
                    " priority, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (mt, pat, tier, ret, pri, note),
                )
            self._safe_commit()
            log.info("_migrate_13_sensitivity: seeded %d rules", len(seeds))
        self._migrate_14_7_router_columns()

    def _migrate_14_7_router_columns(self):
        """Slice 14.7 (2026-05-22): add answered_by + escalation_reason
        columns to chat_messages so each assistant turn carries
        attribution to the layer that produced it (router-Flash vs
        escalated-overseer-Opus) + why an escalation happened. Fresh
        installs get the columns via OVERSEER_SCHEMA_SQL; existing
        installs (.25) need ALTERs."""
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(chat_messages)"
        ).fetchall()}
        for col in ("answered_by", "escalation_reason"):
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE chat_messages ADD COLUMN "
                    f"{col} TEXT NOT NULL DEFAULT ''"
                )
        self._safe_commit()
        self._migrate_14_7_3_category_column()

    def _migrate_14_7_3_category_column(self):
        """Slice 14.7.3 (2026-05-26): add category column to
        imported_sessions for work / cortex / personal / unclassified
        tagging. Powers the [WORK] / [CORTEX] / [PERSONAL] section
        split in temporal narrative prompts. Set by:
          - rule-based classifier (cwd patterns + sensitivity) on
            schema migrate (one-time backfill of cwd-signal rows)
          - LLM classifier (Flash) for the web-AI bulk (no cwd)
          - manual override via /imports/set-category endpoint
        Default '' = unclassified.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(imported_sessions)"
        ).fetchall()}
        if "category" not in cols:
            self._conn.execute(
                "ALTER TABLE imported_sessions ADD COLUMN "
                "category TEXT NOT NULL DEFAULT ''"
            )
            self._conn.execute(
                "ALTER TABLE imported_sessions ADD COLUMN "
                "category_set_by TEXT NOT NULL DEFAULT ''"
            )
            self._conn.execute(
                "ALTER TABLE imported_sessions ADD COLUMN "
                "category_set_at TEXT NOT NULL DEFAULT ''"
            )
            self._safe_commit()
            log.info("_migrate_14_7_3: category columns added; run "
                     "backfill_categories() to populate cwd-signal rows")
        self._migrate_phase1_pull_events()

    def _validate_sub_agent_tiers(self):
        """Startup check: every distinct tier in sub_agent_tiers must
        resolve to a real model via llm_router.SUB_AGENT_TIER_TO_MODEL.

        Why: if a future refactor renames or drops a tier, existing
        DB rows point at a now-missing key. Dispatch would silently
        fall back to a hard-coded default — silent cost discrepancy.
        Logging the mismatch at boot makes it visible.
        """
        try:
            from llm_router import SUB_AGENT_TIER_TO_MODEL
        except Exception:
            return  # router not importable yet; benign
        rows = self._conn.execute(
            "SELECT DISTINCT model_tier FROM sub_agent_tiers"
        ).fetchall()
        for r in rows:
            tier = r[0]
            if tier and tier not in SUB_AGENT_TIER_TO_MODEL:
                log.error(
                    "sub_agent_tiers contains tier %r which doesn't "
                    "resolve to a model in SUB_AGENT_TIER_TO_MODEL "
                    "(valid: %s). Dispatches will fall back to spec "
                    "defaults silently. Migrate or fix the mapping.",
                    tier, list(SUB_AGENT_TIER_TO_MODEL.keys()),
                )

    def _seed_sub_agent_tiers(self):
        """Seed the sub_agent_tiers table with default rows for the
        known B-agents on first boot. C-agents seed themselves at
        graduation time. Idempotent — INSERT OR IGNORE.

        Defaults locked 2026-05-27 with overseer's input:
        - theme_check  → sonnet (confidence-calibration nuance)
        - project_merge_check → flash (structural comparison, cheap is fine)
        Everything else added later defaults to flash.

        IMPORTANT — DB-wins-after-first-seed rule:
        The B-agent SPEC in code carries `default_tier` for first-seed
        only. After the row exists in sub_agent_tiers, the DB row is
        the source of truth — Tory's overrides via /sub-agents/set-tier
        are NOT overwritten by code defaults on re-seed (INSERT OR
        IGNORE). Don't "fix" the code defaults expecting them to apply
        to existing installs; change the DB row instead.
        """
        defaults = (
            ("b", "theme_check",        "sonnet",
             "Theme confidence calibration needs nuance reading; Flash "
             "pattern-matches 'evidence exists → calibrated' and misses "
             "the overconfident calls (per overseer L99 audit 2026-05-27)."),
            ("b", "project_merge_check", "flash",
             "Structural same/distinct/subproject comparison — Flash "
             "handles this cleanly. Upgrade if Tory rates output poorly."),
        )
        for agent_type, agent_name, tier, notes in defaults:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO sub_agent_tiers "
                    "(agent_type, agent_name, model_tier, tier_set_by, "
                    " notes) VALUES (?, ?, ?, 'default', ?)",
                    (agent_type, agent_name, tier, notes),
                )
            except Exception as e:
                log.warning("seed sub_agent_tier %s/%s failed: %s",
                            agent_type, agent_name, e)
        self._safe_commit()

    def _migrate_pull_events_caller_class(self):
        """Looper iter #2 ship (2026-06-06): add caller_class column +
        backfill existing rows via classify_caller.

        Fresh installs get the column from OVERSEER_SCHEMA_SQL. Existing
        installs (.25) need the additive ALTER + a backfill so the F1
        adoption metric works on historical data.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(pull_events)"
        ).fetchall()}
        if "caller_class" not in cols:
            self._conn.execute(
                "ALTER TABLE pull_events ADD COLUMN "
                "caller_class TEXT NOT NULL DEFAULT ''"
            )
            self._safe_commit()
            log.info(
                "_migrate_pull_events_caller_class: column added")
        # Index lives here (not in schema bootstrap) so the column
        # is guaranteed to exist before the index attempts to
        # reference it.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pull_events_caller_class "
            "ON pull_events(caller_class, pulled_at)"
        )
        self._safe_commit()
        # Backfill any rows with empty caller_class.
        rows = self._conn.execute(
            "SELECT id, caller_id FROM pull_events "
            "WHERE caller_class = ''"
        ).fetchall()
        if rows:
            for row in rows:
                self._conn.execute(
                    "UPDATE pull_events SET caller_class = ? "
                    "WHERE id = ?",
                    (self.classify_caller(row["caller_id"]), row["id"]),
                )
            self._safe_commit()
            log.info(
                "_migrate_pull_events_caller_class: backfilled %d rows",
                len(rows))

    def _migrate_phase1_pull_events(self):
        """Phase 1 (2026-05-27): pull_events + gist_prompts tables.

        Fresh installs get both via CREATE TABLE IF NOT EXISTS in
        OVERSEER_SCHEMA_SQL. Existing installs (.25) pick them up
        additively on the next boot because schema bootstrap runs
        OVERSEER_SCHEMA_SQL on every open(). This migration is a
        safety check.

        gist_prompts is intentionally left empty after the migration.
        The current gist prompt lives in `prompts.session_gist_prompt`
        as a Python FUNCTION (takes session-specific params), not a
        flat string, so seeding from the constant doesn't fit. The
        overseer will author v1 in gist_prompts the first time it
        decides to evolve the prompt based on pull_events signals —
        that's the right moment for the table to become populated.
        """
        row = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='pull_events'"
        ).fetchone()
        if not row:
            log.warning(
                "_migrate_phase1_pull_events: pull_events table missing "
                "after schema bootstrap — corpus drill signals will not "
                "be recorded until this resolves"
            )
        row2 = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='gist_prompts'"
        ).fetchone()
        if not row2:
            log.warning(
                "_migrate_phase1_pull_events: gist_prompts table missing "
                "after schema bootstrap"
            )
        # Chain: sub-agent tier registry seed (2026-05-27).
        self._seed_sub_agent_tiers()
        # Then validate every tier still resolves to a real model.
        self._validate_sub_agent_tiers()
        # Chain: pull_events caller_class (2026-06-06, looper iter #2).
        self._migrate_pull_events_caller_class()
        # Phase 1d (2026-05-27): wire gist→prompt linkage live.
        self._migrate_phase1d_gist_prompt_link()

    def _migrate_phase1d_gist_prompt_link(self):
        """Phase 1d (2026-05-27): summaries_gist.prompt_version_id +
        seed gist_prompts v1 with the current canonical session prompt.

        Closes Phase 1 of the three-layer architecture seed by making
        the gist→prompt linkage live. Future regenerations (Phase 5
        refinement loop) author v2+ as gist_prompts rows; the linkage
        tracks which gists were produced by which prompt version, so
        consumers' drill-past signals (pull_events) can attribute back
        to a specific prompt version and drive a revision proposal.

        Idempotent — safe to re-run.
        """
        # 1. ALTER summaries_gist for existing installs.
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(summaries_gist)"
        ).fetchall()}
        if "prompt_version_id" not in cols:
            self._conn.execute(
                "ALTER TABLE summaries_gist ADD COLUMN "
                "prompt_version_id INTEGER"
            )
            self._safe_commit()

        # 2. Seed v1 of gist_prompts with the canonical session prompt
        #    template if the table is still empty. Captured by hand
        #    from prompts.session_gist_prompt as of 2026-05-27. Future
        #    revisions get authored by overseer; this row anchors the
        #    baseline for refinement-loop comparators.
        row = self._conn.execute(
            "SELECT id FROM gist_prompts WHERE version_label = ?",
            ("v1",)
        ).fetchone()
        if not row:
            seed_text = (
                "You are summarizing a single Cortex session into ONE "
                "LINE that captures THE CHANGE.\n\n"
                "What did this session change about the user's standing "
                "situation? What's now true that wasn't true before, "
                "or what's now untrue that was? Drop everything the "
                "user already knew, already had, or already believed "
                "before this session. If nothing changed, say that "
                "plainly — 'no net change' is a valid one-line gist."
                "\n\n"
                "Don't describe what the assistant did. Describe what "
                "shifted for the human.\n\n"
                "Session: {sid}\nStarted: {started}\nEnded: {ended}\n"
                "Platform: {platform}\n\n"
                "Notes from this session ({n_total} total{n_shown}):"
                "\n{body}\n\n"
                "Write only the one-sentence gist focused on the "
                "change. No preamble. No quotes. If nothing changed, "
                "write 'No net change in user's standing situation.'"
            )
            self.add_gist_prompt(
                version_label="v1",
                prompt_text=seed_text,
                rationale=(
                    "Seed snapshot of session_gist_prompt as of "
                    "2026-05-27 (Phase 1d). Captures the canonical "
                    "CHANGE-focused template the loop has used since "
                    "the F1 reader surface shipped. Future revisions "
                    "(v2+) will be authored by overseer based on "
                    "pull_events drill-past signals per the "
                    "three_layer_architecture_design_seed.md "
                    "refinement loop."
                ),
                make_active=True,
            )
        self._migrate_corpus_decisions_links()

    def _migrate_corpus_decisions_links(self):
        """Looper datamining pass 4-5 (2026-06-07): additive enrichment
        columns on corpus_decisions — people + themes (pass 4, links UP into
        the abstraction graph) and raw_session_id (pass 5, links DOWN to the
        raw imported_session). Fresh installs get them via the CREATE TABLE in
        OVERSEER_SCHEMA_SQL; existing installs (.25) pick them up here.
        Idempotent. The table itself may not exist yet on a brand-new install
        at the moment this runs, but the schema bootstrap creates it first, so
        the PRAGMA is safe."""
        row = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='corpus_decisions'"
        ).fetchone()
        if not row:
            return
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(corpus_decisions)"
        ).fetchall()}
        for col in ("people", "themes", "raw_session_id"):
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE corpus_decisions ADD COLUMN {col} TEXT"
                )
        self._safe_commit()
        self._migrate_vector_index()

    def _migrate_vector_index(self):
        """Vector index CP1 (2026-06-10): vec_gists virtual table +
        embedding_meta. Lives OUTSIDE the bootstrap SQL because vec0
        tables require the sqlite-vec extension, which may be absent
        (vec_available=False -> skip everything; the plugin runs fine
        without vectors). Cosine distance declared on the column so
        semantic_neighbors returns 0..2 cosine distances directly.

        embedding_meta pins the model: vectors from different models
        are not comparable, so a model change requires a full re-embed
        (drop + backfill). ensure_embedding_model() enforces this.

        Only the vec_gists virtual table + embedding_meta are gated on
        vec_available: a vec0 table cannot be created without sqlite-vec.
        The rest of the migration chain (_migrate_15_missions and its
        tail _migrate_taxonomy_gist_axes) is vec-independent schema and
        MUST still run when vec is absent - it is called unconditionally
        below. Early-returning here used to silently skip every later
        link, so fresh installs without sqlite-vec (Windows dev, CI) came
        up missing the missions + taxonomy schema, and a future Pi break
        of sqlite-vec (e.g. a piwheels upgrade) would silently stall the
        chain tail. Guard the vec DDL only, never the chain.
        """
        if self.vec_available:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS embedding_meta ("
                "  id INTEGER PRIMARY KEY CHECK (id = 1),"
                "  model TEXT NOT NULL,"
                "  dim INTEGER NOT NULL,"
                "  created_at TEXT NOT NULL DEFAULT (datetime('now'))"
                ")"
            )
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_gists USING vec0("
                "  gist_id INTEGER PRIMARY KEY,"
                "  embedding float[384] distance_metric=cosine"
                ")"
            )
            self._safe_commit()
        self._migrate_15_missions()

    def _migrate_15_missions(self):
        """Slice 15 CP1 (2026-06-10): Project Missions events spine.

        Missions ARE c_agents rows with mission_focus set - reuses the
        existing C registry, promotion flow, and scheduled-run step.
        mission_project is TEXT (the corpus keys projects by tag/name,
        not integer id - deliberate deviation from the seed).
        project_events + mission_subscriptions are the trigger system
        the seed called the missing piece; the semantic gate at match
        time is what the vector index unblocked.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(c_agents)").fetchall()}
        for col, decl in (
            ("mission_project", "TEXT"),
            ("dispatch_authority", "TEXT NOT NULL DEFAULT 'read_only'"),
            ("budget_usd_per_day", "REAL NOT NULL DEFAULT 0.10"),
            ("mission_focus", "TEXT"),
            ("mission_scratchpad", "TEXT"),
        ):
            if col not in cols:
                self._conn.execute(
                    "ALTER TABLE c_agents ADD COLUMN {} {}".format(
                        col, decl))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS project_events ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  kind TEXT NOT NULL,"
            "  project TEXT NOT NULL DEFAULT '',"
            "  payload_json TEXT NOT NULL DEFAULT '{}',"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            "  processed_at TEXT"
            ")"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_events_unprocessed "
            "ON project_events(processed_at, id)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS mission_subscriptions ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  mission_id INTEGER NOT NULL,"
            "  event_kind TEXT NOT NULL,"
            "  min_similarity REAL NOT NULL DEFAULT 0.55,"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            "  UNIQUE(mission_id, event_kind)"
            ")"
        )
        self._safe_commit()
        self._migrate_taxonomy_gist_axes()

    def _migrate_taxonomy_gist_axes(self):
        """Taxonomy build (2026-06-13): the Modality + Lens axes on gists.

        Modality is half the integrity pair (Provenance + Modality); Lens
        carries the 6 controlled interpretive lenses. Both are stamped by
        scripts/taxonomy_reprocess.py reading the RAW transcript (the gist
        body optimizes for THE CHANGE and discarded the lens signal at
        generation time). axis_processed_at is NULL until reprocessed, so
        the reprocessor is resumable and cost-capped over many runs.
        Additive + idempotent ALTER TABLE for the live .25 install.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(summaries_gist)").fetchall()}
        for col, decl in (
            ("modality", "TEXT"),
            ("lens", "TEXT"),
            ("axis_processed_at", "TEXT"),
        ):
            if col not in cols:
                self._conn.execute(
                    "ALTER TABLE summaries_gist ADD COLUMN {} {}".format(
                        col, decl))
                log.info("_migrate_taxonomy_gist_axes: added %s column", col)
        self._safe_commit()

    def _migrate_tech_nocase(self):
        """Tech skills/rules (2026-07-12): the first deploy shipped
        tech_skills.name / tech_rules.title UNIQUE with BINARY
        collation while dedup is case-insensitive in code, so the
        constraint did not enforce the natural key and concurrent
        writers could create case-variant duplicates. Rebuild both
        tables with UNIQUE COLLATE NOCASE, preserving rows AND ids
        (tech_skill_log FKs stay valid). CREATE TABLE IF NOT EXISTS
        never retrofits, hence this migration
        (memory/feedback_create_table_if_not_exists_drift.md).
        Called directly from __init__; idempotent (no-ops once the
        table SQL carries NOCASE)."""
        specs = {
            "tech_skills": (
                ("id", "name", "proficiency", "summary", "tools",
                 "created_at", "updated_at"),
                "CREATE TABLE tech_skills ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " name TEXT NOT NULL UNIQUE COLLATE NOCASE,"
                " proficiency TEXT NOT NULL DEFAULT '',"
                " summary TEXT NOT NULL DEFAULT '',"
                " tools TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                " updated_at TEXT NOT NULL DEFAULT (datetime('now')))",
                "CREATE INDEX IF NOT EXISTS idx_tech_skills_name_lower"
                " ON tech_skills(LOWER(name))"),
            "tech_rules": (
                ("id", "title", "rule", "stack", "situation",
                 "went_wrong", "what_changed", "rationale", "status",
                 "source", "created_at", "updated_at"),
                "CREATE TABLE tech_rules ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " title TEXT NOT NULL UNIQUE COLLATE NOCASE,"
                " rule TEXT NOT NULL,"
                " stack TEXT NOT NULL DEFAULT '',"
                " situation TEXT NOT NULL DEFAULT '',"
                " went_wrong TEXT NOT NULL DEFAULT '',"
                " what_changed TEXT NOT NULL DEFAULT '',"
                " rationale TEXT NOT NULL DEFAULT '',"
                " status TEXT NOT NULL DEFAULT 'active',"
                " source TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                " updated_at TEXT NOT NULL DEFAULT (datetime('now')))",
                "CREATE INDEX IF NOT EXISTS idx_tech_rules_status"
                " ON tech_rules(status, updated_at)"),
        }
        for table, (cols, create_sql, index_sql) in specs.items():
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name=?", (table,)).fetchone()
            if not row or "NOCASE" in (row[0] or "").upper():
                continue
            col_list = ", ".join(cols)
            with self._write_lock:
                rows = self._conn.execute(
                    "SELECT {} FROM {}".format(col_list, table)
                ).fetchall()
                # FKs OFF so dropping tech_skills cannot cascade-
                # delete tech_skill_log rows; restored right after.
                # (PRAGMA is a no-op inside a transaction, so it runs
                # before the DROP opens one.)
                self._conn.execute("PRAGMA foreign_keys=OFF")
                try:
                    self._conn.execute("DROP TABLE {}".format(table))
                    self._conn.execute(create_sql)
                    marks = ", ".join("?" for _ in cols)
                    for r in rows:
                        # OR IGNORE: a case-variant duplicate pair
                        # from the pre-NOCASE window keeps only the
                        # first (lower-id) row.
                        self._conn.execute(
                            "INSERT OR IGNORE INTO {} ({}) VALUES ({})"
                            .format(table, col_list, marks), tuple(r))
                    self._conn.execute(index_sql)
                    self._safe_commit()
                finally:
                    self._conn.execute("PRAGMA foreign_keys=ON")
            log.info("_migrate_tech_nocase: rebuilt %s with NOCASE"
                     " (%d rows)", table, len(rows))

    # Called directly from __init__, NOT chained off the taxonomy
    # migration: _migrate_vector_index early-returns when sqlite-vec
    # is unavailable, which would silently break every later link.
    def _migrate_chat_threads(self):
        """Agent harness (2026-07-10): chat gains threads.

        1. chat_threads + chat_prompts tables exist via CREATE TABLE
           IF NOT EXISTS in the schema (fresh installs get thread_id
           in the CREATE TABLE too).
        2. Existing installs (.25) need the thread_id column ALTERed
           onto chat_messages.
        3. Any pre-thread rows (thread_id=0) are adopted into a
           single legacy thread so history is preserved verbatim.
        4. The active-thread pointer is seeded if missing.
        Idempotent: reruns no-op once rows are adopted.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(chat_messages)").fetchall()}
        if "thread_id" not in cols:
            self._conn.execute(
                "ALTER TABLE chat_messages ADD COLUMN "
                "thread_id INTEGER NOT NULL DEFAULT 0")
            log.info("_migrate_chat_threads: added thread_id column")
        # Unconditional: fresh installs get the column via CREATE
        # TABLE (skipping the ALTER above) but still need the index.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_thread "
            "ON chat_messages(thread_id, id)")
        orphans = self._conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE thread_id = 0"
        ).fetchone()[0]
        if orphans:
            # thread_id=0 is also the column DEFAULT, so 0-rows can
            # reappear after a rollback/re-deploy or from an external
            # writer that omits the column. Adopt into the OLDEST
            # existing thread instead of minting a duplicate 'Main
            # thread', and never hijack a valid active pointer.
            row = self._conn.execute(
                "SELECT id FROM chat_threads ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                legacy_id = row["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO chat_threads (title) VALUES (?)",
                    ("Main thread",))
                legacy_id = cur.lastrowid
            self._conn.execute(
                "UPDATE chat_messages SET thread_id = ? "
                "WHERE thread_id = 0", (legacy_id,))
            ptr = self.get_overseer_state("chat_active_thread_id")
            ptr_valid = False
            if ptr is not None:
                try:
                    ptr_valid = self._conn.execute(
                        "SELECT 1 FROM chat_threads WHERE id = ?",
                        (int(ptr),)).fetchone() is not None
                except (TypeError, ValueError):
                    ptr_valid = False
            if not ptr_valid:
                self.set_overseer_state(
                    "chat_active_thread_id", legacy_id)
            log.info("_migrate_chat_threads: adopted %d messages into "
                     "thread %d", orphans, legacy_id)
        self._safe_commit()

    # ── Slice 15: mission events ─────────────────────────────────

    def publish_event(self, kind, *, project="", payload=None):
        """Publish a project event for the missions step to drain.
        Cheap insert; callers wrap in try/except (publishing must
        never break the publishing code path)."""
        cur = self._conn.execute(
            "INSERT INTO project_events (kind, project, payload_json) "
            "VALUES (?, ?, ?)",
            (kind, project or "",
             json.dumps(payload or {}, separators=(",", ":"),
                        default=str)))
        self._safe_commit()
        return cur.lastrowid

    def unprocessed_events(self, limit=50):
        rows = self._conn.execute(
            "SELECT id, kind, project, payload_json, created_at "
            "FROM project_events WHERE processed_at IS NULL "
            "ORDER BY id LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def mark_events_processed(self, ids):
        """Anchor-mark rule: events get marked processed whether or
        not anything subscribed - never rescan the same window."""
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self._conn.execute(
            "UPDATE project_events SET processed_at = datetime('now') "
            "WHERE id IN ({})".format(marks), [int(i) for i in ids])
        self._safe_commit()

    def mission_subscriptions_for(self, kind):
        rows = self._conn.execute(
            "SELECT s.id AS sub_id, s.event_kind, s.min_similarity, "
            "c.id AS mission_id, c.name, c.mission_project, "
            "c.mission_focus, c.dispatch_authority "
            "FROM mission_subscriptions s "
            "JOIN c_agents c ON c.id = s.mission_id "
            "WHERE s.event_kind = ? AND c.status = 'active' "
            "AND c.mission_focus IS NOT NULL", (kind,)).fetchall()
        return [dict(r) for r in rows]

    def create_mission(self, *, name, focus, project="",
                       event_kinds=None, min_similarity=0.55):
        """Slice 15 CP2: one-call mission creation. cadence_minutes
        is set huge + last_run_at fresh so the scheduled C runner
        never fires missions as C agents (CP1 lesson)."""
        kinds = [k for k in (event_kinds or ["gist.created"]) if k]
        try:
            cur = self._conn.execute(
                "INSERT INTO c_agents (name, graduated_from_b_name, "
                "cadence_minutes, system_prompt, status, last_run_at, "
                "mission_project, mission_focus, dispatch_authority) "
                "VALUES (?, 'mission', 525600, ?, 'active', "
                "datetime('now'), ?, ?, 'read_only')",
                (name, "Mission: " + focus, project or "", focus))
        except sqlite3.IntegrityError:
            return {"ok": False,
                    "error": "mission name already exists: " + name}
        mid = cur.lastrowid
        for k in kinds:
            self._conn.execute(
                "INSERT OR IGNORE INTO mission_subscriptions "
                "(mission_id, event_kind, min_similarity) "
                "VALUES (?, ?, ?)", (mid, k, float(min_similarity)))
        self._safe_commit()
        return {"ok": True, "mission_id": mid, "name": name,
                "subscribed": kinds,
                "min_similarity": float(min_similarity)}

    def list_missions(self):
        rows = self._conn.execute(
            "SELECT id, name, status, mission_project, mission_focus, "
            "dispatch_authority, budget_usd_per_day, created_at, "
            "mission_scratchpad FROM c_agents "
            "WHERE mission_focus IS NOT NULL ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            subs = self._conn.execute(
                "SELECT event_kind, min_similarity "
                "FROM mission_subscriptions WHERE mission_id = ?",
                (r["id"],)).fetchall()
            d["subscriptions"] = [dict(s) for s in subs]
            pad = d.pop("mission_scratchpad") or ""
            d["scratchpad_tail"] = pad[-500:]
            out.append(d)
        return out

    def set_mission_status(self, name, status):
        """Retire/revive a mission by name. Scratchpad untouched, so
        a revived mission keeps its full prior context (seed
        acceptance criterion). Returns affected row count."""
        cur = self._conn.execute(
            "UPDATE c_agents SET status = ? "
            "WHERE name = ? AND mission_focus IS NOT NULL",
            (status, name))
        self._safe_commit()
        return cur.rowcount

    def append_mission_scratchpad(self, mission_id, line, max_chars=8000):
        """Append to the mission's lightweight scratchpad, keeping the
        tail. The scratchpad is the CP1 landing zone (seed checkpoint
        2: lightweight first)."""
        row = self._conn.execute(
            "SELECT mission_scratchpad FROM c_agents WHERE id = ?",
            (int(mission_id),)).fetchone()
        current = (row["mission_scratchpad"] or "") if row else ""
        updated = (current + line)[-max_chars:]
        self._conn.execute(
            "UPDATE c_agents SET mission_scratchpad = ? WHERE id = ?",
            (updated, int(mission_id)))
        self._safe_commit()

    def _migrate_9_6_notification_actions(self):
        """Slice 9.6 CP1 (2026-05-19): notifications gain actions_json
        column for per-notification custom action buttons. Fresh
        installs get it via OVERSEER_SCHEMA_SQL; existing installs
        (the .25 we deploy to) need an ALTER TABLE here.

        notification_responses table is created by CREATE TABLE IF
        NOT EXISTS in the schema — no migration needed for it on
        existing installs.
        """
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(notifications)"
        ).fetchall()}
        if "actions_json" not in cols:
            self._conn.execute(
                "ALTER TABLE notifications ADD COLUMN actions_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
            self._safe_commit()
        self._migrate_9_8_imported_redacted()

    # ── overseer_state ──────────────────────────────────────────

    def get_overseer_state(self, key, default=None):
        row = self._conn.execute(
            "SELECT value FROM overseer_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_overseer_state(self, key, value):
        self._conn.execute(
            "INSERT INTO overseer_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now')",
            (key, str(value)),
        )
        self._safe_commit()

    def delete_overseer_state(self, key):
        """Slice 14.7.2 (2026-05-26): delete a state row. Used by the
        daily-budget override expiry path (override clears at the
        local-midnight rollover handled in DailyBudget._refresh_date).
        """
        self._conn.execute(
            "DELETE FROM overseer_state WHERE key = ?", (key,))
        self._safe_commit()

    # ── raw_pointers ────────────────────────────────────────────

    def add_raw_pointer(self, source_kind, source_path="", source_id="", notes=""):
        cur = self._conn.execute(
            "INSERT INTO raw_pointers (source_kind, source_path, source_id, notes) "
            "VALUES (?, ?, ?, ?)",
            (source_kind, source_path, source_id, notes),
        )
        self._safe_commit()
        return cur.lastrowid

    # ── tags ────────────────────────────────────────────────────

    def tag(self, table_name, row_id, tag_value):
        """Attach a tag to a row. Idempotent (UNIQUE constraint)."""
        try:
            self._conn.execute(
                "INSERT INTO tags (table_name, row_id, tag) VALUES (?, ?, ?)",
                (table_name, row_id, tag_value),
            )
            self._safe_commit()
        except sqlite3.IntegrityError:
            pass  # already tagged

    def tag_many(self, table_name, row_id, tags_iter):
        for t in tags_iter or []:
            t = (t or "").strip()
            if t:
                self.tag(table_name, row_id, t)

    def get_tags_for(self, table_name, row_id):
        rows = self._conn.execute(
            "SELECT tag FROM tags WHERE table_name = ? AND row_id = ? ORDER BY tag",
            (table_name, row_id),
        ).fetchall()
        return [r["tag"] for r in rows]

    # ── vector index (CP1/CP2, 2026-06-10) ──────────────────────

    def ensure_embedding_model(self, model, dim):
        """Pin the embedding model. Returns True if `model` matches the
        pinned one (or pins it on first call). False means a DIFFERENT
        model produced the existing vectors — mixing is refused; drop
        and re-embed instead (POST /vector/backfill {"reembed": true}).
        """
        if not self.vec_available:
            return False
        row = self._conn.execute(
            "SELECT model, dim FROM embedding_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO embedding_meta (id, model, dim) "
                "VALUES (1, ?, ?)", (model, int(dim)))
            self._safe_commit()
            return True
        return row["model"] == model and row["dim"] == int(dim)

    @staticmethod
    def _vec_blob(vec):
        import struct
        return struct.pack("{}f".format(len(vec)), *vec)

    def upsert_gist_embedding(self, gist_id, vec):
        """Store one gist vector. vec0 has no UPSERT; delete + insert."""
        if not self.vec_available:
            return
        with self._write_lock:
            self._conn.execute(
                "DELETE FROM vec_gists WHERE gist_id = ?", (int(gist_id),))
            self._conn.execute(
                "INSERT INTO vec_gists (gist_id, embedding) VALUES (?, ?)",
                (int(gist_id), self._vec_blob(vec)))
            self._conn.commit()

    def unembedded_gist_ids(self, limit=64):
        if not self.vec_available:
            return []
        rows = self._conn.execute(
            "SELECT id FROM summaries_gist WHERE id NOT IN "
            "(SELECT gist_id FROM vec_gists) ORDER BY id LIMIT ?",
            (int(limit),)).fetchall()
        return [r["id"] for r in rows]

    def embed_gists(self, gist_ids):
        """Embed the given gists via the local llama-embed service and
        store the vectors. Best-effort: returns the number embedded;
        0 means the service was down or the model pin mismatched, and
        the gists stay unembedded for the next backfill pass."""
        if not self.vec_available or not gist_ids:
            return 0
        from embeddings import embed_texts, MODEL_NAME, DIM
        if not self.ensure_embedding_model(MODEL_NAME, DIM):
            log.error("embedding model mismatch; refusing to mix "
                      "vectors (re-embed required)")
            return 0
        marks = ",".join("?" for _ in gist_ids)
        rows = self._conn.execute(
            "SELECT id, body FROM summaries_gist WHERE id IN "
            "({})".format(marks), [int(g) for g in gist_ids]).fetchall()
        if not rows:
            return 0
        vecs = embed_texts([r["body"] or "" for r in rows])
        if vecs is None:
            return 0
        for row, vec in zip(rows, vecs):
            self.upsert_gist_embedding(row["id"], vec)
        return len(rows)

    def semantic_neighbors(self, query_vec, k=10):
        """KNN over gist vectors. Returns [{gist_id, distance}] ranked
        nearest-first; distance is cosine distance (0 identical .. 2
        opposite)."""
        if not self.vec_available:
            return []
        rows = self._conn.execute(
            "SELECT gist_id, distance FROM vec_gists "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (self._vec_blob(query_vec), int(k))).fetchall()
        return [{"gist_id": r["gist_id"], "distance": r["distance"]}
                for r in rows]

    def drop_all_embeddings(self):
        """Model-swap path: clear every vector + the model pin so the
        next backfill re-embeds the whole corpus with the new model."""
        if not self.vec_available:
            return
        with self._write_lock:
            self._conn.execute("DELETE FROM vec_gists")
            self._conn.execute("DELETE FROM embedding_meta")
            self._conn.commit()

    def vector_status(self):
        if not self.vec_available:
            return {"available": False}
        total = self._conn.execute(
            "SELECT COUNT(*) FROM summaries_gist").fetchone()[0]
        embedded = self._conn.execute(
            "SELECT COUNT(*) FROM vec_gists").fetchone()[0]
        meta = self._conn.execute(
            "SELECT model, dim, created_at FROM embedding_meta "
            "WHERE id = 1").fetchone()
        return {
            "available": True,
            "model": meta["model"] if meta else None,
            "dim": meta["dim"] if meta else None,
            "total_gists": total,
            "embedded": embedded,
            "coverage_pct": round(100.0 * embedded / total, 1)
            if total else 0.0,
        }

    # ── summaries_gist ──────────────────────────────────────────

    def add_gist(self, body, *, period_label="", period_start=None,
                 period_end=None, confidence="med", raw_pointer_id=None,
                 prompt_version_id=None, tags=None):
        # Phase 1d (2026-05-27): if no explicit prompt_version_id was
        # passed, auto-link to the currently-active gist_prompts row
        # so refinement-loop signals (pull_events drill-past) can
        # attribute back to a specific prompt version. Callers that
        # don't fit the standard session-gist path can pass
        # prompt_version_id=0 or a specific id to override.
        if prompt_version_id is None:
            active = self.get_active_gist_prompt()
            if active:
                prompt_version_id = active["id"]
        cur = self._conn.execute(
            "INSERT INTO summaries_gist (period_label, period_start, "
            "period_end, body, confidence, raw_pointer_id, "
            "prompt_version_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (period_label, period_start, period_end, body,
             _norm_confidence(confidence), raw_pointer_id,
             prompt_version_id),
        )
        self._safe_commit()
        gid = cur.lastrowid
        # Phase 1d: bump the prompt's gists_generated counter so the
        # refinement loop has a denominator for drill-past ratios.
        if prompt_version_id:
            self._conn.execute(
                "UPDATE gist_prompts SET gists_generated = "
                "gists_generated + 1 WHERE id = ?",
                (int(prompt_version_id),),
            )
            self._safe_commit()
        self.tag_many("summaries_gist", gid, tags)
        # Vector index CP2 (2026-06-10): embed-on-write, best effort.
        # Failure never blocks the gist insert; the backfill pass picks
        # up anything missed (acceptance criterion from the seed).
        try:
            self.embed_gists([gid])
        except Exception as e:
            log.warning("embed-on-write failed for gist %s: %s", gid, e)
        return gid

    def recent_gists(self, limit=10):
        rows = self._conn.execute(
            "SELECT * FROM summaries_gist ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_gist(self, gist_id):
        row = self._conn.execute(
            "SELECT * FROM summaries_gist WHERE id = ?", (int(gist_id),),
        ).fetchone()
        return dict(row) if row else None

    # ── Slice 9.2 (overseer ask #2): staleness signals ─────────────
    # The overseer asked to see its own ingest backlog + last-gist
    # freshness in the working_memory artifact so it can tell whether
    # quiet stretches reflect user absence or ingest stall. Both reads
    # are O(table-scan-with-filter); summaries_gist has idx_gist_created.

    def last_successful_gist_at(self) -> str | None:
        """ISO timestamp of the most recent summaries_gist row, or None
        if the table is empty. Used by working_memory so the overseer
        can compute how long it's been since a fresh observation."""
        row = self._conn.execute(
            "SELECT MAX(created_at) AS last FROM summaries_gist"
        ).fetchone()
        return row["last"] if row and row["last"] else None

    def recent_gist_source_distribution(self, recent_n: int = 30) -> dict:
        """Of the most-recent N gists, what's the source/origin breakdown?

        Used by the chat freshness section so the overseer can self-detect
        sampling bias — e.g. "my last 30 gists are all chatgpt-archive
        rollups while 906 grok-com sessions sit unprocessed". The overseer
        flagged this as ask #2-followup; round 3 then learned that gists
        come from two paths and only one of them uses `source:` tags:

          path 1 — import-summary (one gist per imported_session):
            tag `source:<value>` (chatgpt | claude-code | grok-com | grok-twitter)
          path 2 — automation_rollup (one gist per project per period):
            tags `auto`, `automation-rollup`, `project:<value>`. No source: tag.

        So we report a combined "origin" view: source: tags first (true
        per-session content), then project: tags for rollups (aggregate
        signal but at least labeled), then untagged as a final bucket.

        Returns {"window_size": int, "by_origin": {label: count},
                 "untagged": int}. Each origin label is prefixed with its
        tag type, e.g. "source:grok-com" or "rollup:chatgpt-archive"."""
        rows = self._conn.execute(
            "SELECT id FROM summaries_gist "
            "ORDER BY created_at DESC LIMIT ?",
            (int(recent_n),),
        ).fetchall()
        if not rows:
            return {"window_size": 0, "by_origin": {}, "untagged": 0}
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        tag_rows = self._conn.execute(
            f"SELECT row_id, tag FROM tags "
            f"WHERE table_name = 'summaries_gist' "
            f"  AND row_id IN ({placeholders})",
            ids,
        ).fetchall()
        # Bucket each gist by its strongest origin signal
        by_gist: dict[int, str] = {}
        for tr in tag_rows:
            tag = tr["tag"]
            row_id = tr["row_id"]
            if tag.startswith("source:"):
                # source: wins (it's the per-session content tag)
                by_gist[row_id] = "source:" + tag.split(":", 1)[1]
            elif (tag.startswith("project:")
                  and row_id not in by_gist):
                # project: is the fallback for rollups
                by_gist[row_id] = "rollup:" + tag.split(":", 1)[1]
        by_origin: dict[str, int] = {}
        for origin in by_gist.values():
            by_origin[origin] = by_origin.get(origin, 0) + 1
        return {
            "window_size": len(ids),
            "by_origin": by_origin,
            "untagged": len(ids) - len(by_gist),
        }

    def imported_sessions_queue_stats(self) -> dict:
        """Count of imported_sessions awaiting overseer processing.

        Returns {"total": int, "by_source": {source: count}}. A row counts
        as "unprocessed" if it has no matching row in
        processed_imported_sessions. This is the same condition the
        loop's _summarize_imported_sessions uses to find work."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM imported_sessions i "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM processed_imported_sessions p "
            "  WHERE p.imported_id = i.id"
            ")"
        ).fetchone()
        total = row["n"] if row else 0
        rows = self._conn.execute(
            "SELECT i.source, COUNT(*) AS n FROM imported_sessions i "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM processed_imported_sessions p "
            "  WHERE p.imported_id = i.id"
            ") "
            "GROUP BY i.source"
        ).fetchall()
        by_source = {r["source"]: r["n"] for r in rows}
        return {"total": total, "by_source": by_source}

    def questions_for_evidence(self, evidence_table, evidence_id):
        """Reverse lookup: which open_questions has this row been filed
        against? Used by the drill-down to walk gist → questions."""
        rows = self._conn.execute(
            "SELECT q.id, q.question, q.confidence, q.lifecycle, "
            "  e.contribution, e.reason, e.contributed_at "
            "FROM evidence_for_question e "
            "JOIN open_questions q ON q.id = e.question_id "
            "WHERE e.evidence_table = ? AND e.evidence_id = ? "
            "ORDER BY e.contributed_at DESC",
            (evidence_table, int(evidence_id)),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── summaries_theme ─────────────────────────────────────────

    def add_theme(self, title, body, *, confidence="med",
                  raw_pointer_id=None, tags=None):
        cur = self._conn.execute(
            "INSERT INTO summaries_theme (title, body, confidence, raw_pointer_id) "
            "VALUES (?, ?, ?, ?)",
            (title, body, _norm_confidence(confidence), raw_pointer_id),
        )
        self._safe_commit()
        tid = cur.lastrowid
        self.tag_many("summaries_theme", tid, tags)
        return tid

    def recent_themes(self, limit=10):
        rows = self._conn.execute(
            "SELECT * FROM summaries_theme "
            "ORDER BY last_reinforced_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_theme(self, theme_id):
        row = self._conn.execute(
            "SELECT * FROM summaries_theme WHERE id = ?", (int(theme_id),),
        ).fetchone()
        return dict(row) if row else None

    def gists_for_theme(self, theme_id, *, limit=25):
        """Member gists of a theme (newest first) with bodies for the
        detail drill-down. Backs the theme->gist next_tokens path that
        makes topical themes navigable (looper cycle 2)."""
        rows = self._conn.execute(
            "SELECT tg.gist_id, g.body, g.created_at "
            "FROM theme_gists tg "
            "JOIN summaries_gist g ON g.id = tg.gist_id "
            "WHERE tg.theme_id = ? "
            "ORDER BY g.created_at DESC LIMIT ?",
            (int(theme_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_gists_for_theme(self, theme_id):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM theme_gists WHERE theme_id = ?",
            (int(theme_id),),
        ).fetchone()
        return int(row["n"]) if row else 0

    # ── summaries_episode ───────────────────────────────────────

    def add_episode(self, title, body, *, surface_when="", duration_label="",
                    occurred_at=None, confidence="med",
                    raw_pointer_id=None, tags=None):
        cur = self._conn.execute(
            "INSERT INTO summaries_episode (title, body, surface_when, "
            "duration_label, occurred_at, confidence, raw_pointer_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, body, surface_when, duration_label, occurred_at,
             _norm_confidence(confidence), raw_pointer_id),
        )
        self._safe_commit()
        eid = cur.lastrowid
        self.tag_many("summaries_episode", eid, tags)
        return eid

    def recent_episodes(self, limit=10):
        rows = self._conn.execute(
            "SELECT * FROM summaries_episode ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_episode(self, episode_id):
        row = self._conn.execute(
            "SELECT * FROM summaries_episode WHERE id = ?", (int(episode_id),),
        ).fetchone()
        return dict(row) if row else None

    # ── open_questions ──────────────────────────────────────────

    def add_question(self, question, *, body="", confidence="med",
                     raw_pointer_id=None, tags=None, is_active=True):
        cur = self._conn.execute(
            "INSERT INTO open_questions (question, body, confidence, "
            "raw_pointer_id, is_active) VALUES (?, ?, ?, ?, ?)",
            (question, body, _norm_confidence(confidence),
             raw_pointer_id, 1 if is_active else 0),
        )
        self._safe_commit()
        qid = cur.lastrowid
        self.tag_many("open_questions", qid, tags)
        return qid

    def active_questions(self, limit=20):
        """Returns questions whose lifecycle is active or partially
        answered (the ones surfaced as 'open' to the user). Order: most
        recent evidence first (so questions with new movement bubble up),
        then by first_observed_at."""
        rows = self._conn.execute(
            "SELECT * FROM open_questions WHERE lifecycle IN "
            "('active', 'partially_answered') "
            "ORDER BY COALESCE(last_evidence_at, first_observed_at) DESC, "
            "first_observed_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_questions_by_lifecycle(self, *, lifecycles=None, limit=200):
        """All questions filtered by lifecycle list (default: any state)."""
        sql = "SELECT * FROM open_questions"
        params: list = []
        if lifecycles:
            placeholders = ",".join(["?"] * len(lifecycles))
            sql += " WHERE lifecycle IN ({})".format(placeholders)
            params.extend(lifecycles)
        sql += (" ORDER BY COALESCE(last_evidence_at, first_observed_at) "
                "DESC LIMIT ?")
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_question(self, question_id):
        row = self._conn.execute(
            "SELECT * FROM open_questions WHERE id = ?",
            (int(question_id),),
        ).fetchone()
        return dict(row) if row else None

    VALID_LIFECYCLES = (
        "dormant", "active", "partially_answered",
        "resolved", "abandoned",
    )

    def set_question_lifecycle(self, question_id, lifecycle):
        if lifecycle not in self.VALID_LIFECYCLES:
            raise ValueError(
                "lifecycle must be one of {}".format(self.VALID_LIFECYCLES))
        # Keep is_active in sync for backwards compat
        is_active = 1 if lifecycle in ("active", "partially_answered") else 0
        cur = self._conn.execute(
            "UPDATE open_questions SET lifecycle = ?, is_active = ? "
            "WHERE id = ?",
            (lifecycle, is_active, int(question_id)),
        )
        self._safe_commit()
        return cur.rowcount > 0

    # ── Evidence M:N ────────────────────────────────────────────

    VALID_CONTRIBUTIONS = (
        "supports", "complicates", "answers", "reframes",
    )

    def file_evidence(self, *, question_id, evidence_table, evidence_id,
                      contribution="supports", reason="", confidence="med",
                      contributed_by="auto"):
        """Idempotent. Returns (filed: bool, reactivated: bool).

        - If the (question, evidence) pair is new: filed=True
        - If the question was 'dormant', flips to 'active' and
          reactivated=True (caller can emit a notification)
        - 'answers' contribution moves an active/dormant question to
          'partially_answered' (NEVER auto-flips to 'resolved' — that's
          user-only, since LLM-driven 'this answers it' is too eager)
        """
        if contribution not in self.VALID_CONTRIBUTIONS:
            raise ValueError(
                "contribution must be one of {}".format(
                    self.VALID_CONTRIBUTIONS))
        try:
            self._conn.execute(
                "INSERT INTO evidence_for_question (question_id, "
                "evidence_table, evidence_id, contribution, reason, "
                "confidence, contributed_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(question_id), evidence_table, int(evidence_id),
                 contribution, reason, confidence, contributed_by),
            )
        except sqlite3.IntegrityError:
            # Already filed; not an error
            return (False, False)

        # Update aggregate fields
        self._conn.execute(
            "UPDATE open_questions SET evidence_count = evidence_count + 1, "
            "last_evidence_at = datetime('now') WHERE id = ?",
            (int(question_id),),
        )
        # Lifecycle transitions
        q = self.get_question(question_id)
        reactivated = False
        if q:
            cur_lc = q.get("lifecycle") or "active"
            new_lc = cur_lc
            if cur_lc == "dormant":
                new_lc = "active"
                reactivated = True
            if contribution == "answers" and cur_lc != "resolved":
                new_lc = "partially_answered"
            if new_lc != cur_lc:
                self._conn.execute(
                    "UPDATE open_questions SET lifecycle = ?, "
                    "is_active = ? WHERE id = ?",
                    (new_lc,
                     1 if new_lc in ("active", "partially_answered") else 0,
                     int(question_id)),
                )
        self._safe_commit()
        return (True, reactivated)

    def unfile_evidence(self, *, question_id, evidence_table, evidence_id):
        cur = self._conn.execute(
            "DELETE FROM evidence_for_question WHERE question_id = ? "
            "AND evidence_table = ? AND evidence_id = ?",
            (int(question_id), evidence_table, int(evidence_id)),
        )
        if cur.rowcount > 0:
            # Recompute count from scratch (cheap, exact)
            self._conn.execute(
                "UPDATE open_questions SET evidence_count = ("
                "  SELECT COUNT(*) FROM evidence_for_question "
                "  WHERE question_id = ?) WHERE id = ?",
                (int(question_id), int(question_id)),
            )
            self._safe_commit()
        return cur.rowcount > 0

    def explorer_graph(self, *, max_nodes=200):
        """Polish slice: assemble the data the Explorer renders.

        Returns {nodes: [...], edges: [...], stats: {...}} where:

        - Each node has {id (token), type, label, confidence, size_hint,
          metadata}. Token IDs reuse the 3g drill-down format so the Hub
          can pass them straight to the existing DetailCard.
        - Each edge has {source, target, kind, contribution?, label}.
          Edge kinds: 'evidence' (gist/episode/theme → question via
          evidence_for_question), 'derived_from' (pattern/drift → its
          source gist via raw_pointer_id), 'in_project' (gist → project
          via tag).

        Node selection is deliberately lean for CP1:
          - questions: ALL with lifecycle in (active, partially_answered)
          - patterns: top recent_patterns (by last_observed_at)
          - drift_observations: top recent_drift
          - themes: top recent_themes
          - episodes: top recent_episodes
          - gists: ONLY those with at least one evidence_for_question row
          - projects: distinct project tags from imported_project_settings
            where treat_as != 'ignore'

        max_nodes is a soft cap — we keep questions+patterns+drift+
        themes+episodes always (small) and trim filed-gists/projects if
        over budget.
        """
        nodes: list[dict] = []
        edges: list[dict] = []
        seen_ids: set[str] = set()

        def add_node(node: dict) -> None:
            if node["id"] in seen_ids:
                return
            seen_ids.add(node["id"])
            nodes.append(node)

        def add_edge(source: str, target: str, *, kind: str,
                     label: str = "", contribution: str = "") -> None:
            if source not in seen_ids or target not in seen_ids:
                return  # silently drop dangling edges
            edges.append({
                "source": source, "target": target,
                "kind": kind, "label": label,
                "contribution": contribution,
            })

        # ── Questions (active/partially_answered) ─────────────
        for q in self.all_questions_by_lifecycle(
                lifecycles=("active", "partially_answered"), limit=100):
            qid = "q:{}".format(q["id"])
            add_node({
                "id": qid, "type": "question",
                "label": (q.get("question") or "")[:80],
                "confidence": q.get("confidence") or "med",
                "size_hint": int(q.get("evidence_count") or 0),
                "last_seen": q.get("last_evidence_at")
                              or q.get("last_observed_at")
                              or q.get("first_observed_at"),
                "tags": self.get_tags_for("open_questions", q["id"]),
                "metadata": {
                    "lifecycle": q.get("lifecycle"),
                    "evidence_count": q.get("evidence_count"),
                    "last_evidence_at": q.get("last_evidence_at"),
                },
            })

        # ── Patterns ──────────────────────────────────────────
        for p in self.recent_patterns(limit=40):
            pid = "p:{}".format(p["id"])
            add_node({
                "id": pid, "type": "pattern",
                "label": (p.get("name") or "")[:80],
                "confidence": p.get("confidence") or "med",
                "size_hint": int(p.get("occurrences") or 1),
                "last_seen": p.get("last_observed_at"),
                "tags": self.get_tags_for("patterns", p["id"]),
                "metadata": {
                    "last_observed_at": p.get("last_observed_at"),
                    "occurrences": p.get("occurrences"),
                },
            })

        # ── Drift ─────────────────────────────────────────────
        for d in self.recent_drift(limit=40):
            did = "d:{}".format(d["id"])
            add_node({
                "id": did, "type": "drift",
                "label": (d.get("body") or "")[:80],
                "confidence": d.get("confidence") or "med",
                "size_hint": 2,
                "last_seen": d.get("observed_at"),
                "tags": self.get_tags_for("drift_observations", d["id"]),
                "metadata": {
                    "direction": d.get("direction"),
                    "observed_at": d.get("observed_at"),
                },
            })

        # ── Themes ────────────────────────────────────────────
        for t in self.recent_themes(limit=20):
            tid = "t:{}".format(t["id"])
            add_node({
                "id": tid, "type": "theme",
                "label": (t.get("title") or "")[:80],
                "confidence": t.get("confidence") or "med",
                "size_hint": 3,
                "last_seen": t.get("last_reinforced_at"),
                "tags": self.get_tags_for("summaries_theme", t["id"]),
                "metadata": {
                    "last_reinforced_at": t.get("last_reinforced_at"),
                },
            })

        # ── Episodes ──────────────────────────────────────────
        for e in self.recent_episodes(limit=20):
            eid = "e:{}".format(e["id"])
            add_node({
                "id": eid, "type": "episode",
                "label": (e.get("title") or "")[:80],
                "confidence": e.get("confidence") or "med",
                "size_hint": 2,
                "last_seen": e.get("occurred_at") or e.get("created_at"),
                "tags": self.get_tags_for("summaries_episode", e["id"]),
                "metadata": {
                    "duration_label": e.get("duration_label"),
                    "occurred_at": e.get("occurred_at"),
                },
            })

        # ── Filed gists (those referenced by evidence_for_question) ──
        # Only include the gists that actually serve as evidence — the
        # rest would be visual noise on the canvas. They're still
        # accessible via the drill-down token system.
        filed_gist_rows = self._conn.execute(
            "SELECT DISTINCT g.id, g.body, g.confidence, g.created_at, "
            "g.period_label "
            "FROM summaries_gist g "
            "JOIN evidence_for_question e "
            "  ON e.evidence_table = 'summaries_gist' "
            "  AND e.evidence_id = g.id "
            "ORDER BY g.created_at DESC "
            "LIMIT 100"
        ).fetchall()
        for g in filed_gist_rows:
            gid = "g:{}".format(g["id"])
            add_node({
                "id": gid, "type": "gist",
                "label": (g["body"] or "")[:80],
                "confidence": g["confidence"] or "med",
                "size_hint": 1,
                "last_seen": g["created_at"],
                "tags": self.get_tags_for("summaries_gist", g["id"]),
                "metadata": {
                    "period_label": g["period_label"],
                    "created_at": g["created_at"],
                },
            })

        # ── Projects (Slice 4: pull from project_summaries) ──────
        # CP1a/CP1b made project_summaries the canonical place for
        # per-project rollup data; the old imported_project_settings
        # table only held classification opinions and missed every
        # project the user hadn't explicitly classified (which was
        # ~43 of 47 projects). Pull from project_summaries so the
        # graph reflects the actual project landscape.
        #
        # size_hint scales with active hours so the projects the user
        # has actually invested in render as bigger discs. Cap so a
        # 1000h project doesn't dwarf the canvas.
        # tags include the treat_as classification when one exists,
        # plus a 'dormant' marker (last active >60d) so the frontend
        # can fade them slightly.
        try:
            proj_summaries = self._conn.execute(
                "SELECT s.project, s.last_active_at, "
                "       s.active_minutes_total, s.session_count, "
                "       s.narrative, "
                "       COALESCE(c.treat_as, 'auto') AS treat_as "
                "FROM project_summaries s "
                "LEFT JOIN imported_project_settings c "
                "  ON c.project = s.project "
                "WHERE s.project != '' "
                "  AND COALESCE(c.treat_as, 'auto') != 'ignore'"
            ).fetchall()
        except sqlite3.OperationalError:
            # Pre-Slice-4 install — fall back to the old shape.
            proj_summaries = self._conn.execute(
                "SELECT project, '' AS last_active_at, "
                "       0 AS active_minutes_total, 0 AS session_count, "
                "       '' AS narrative, treat_as "
                "FROM imported_project_settings "
                "WHERE project != '' AND treat_as != 'ignore'"
            ).fetchall()

        from datetime import datetime, timezone, timedelta
        cutoff_60d = (datetime.now(timezone.utc) - timedelta(days=60)
                      ).strftime("%Y-%m-%d")
        for r in proj_summaries:
            ptag = r["project"]
            pid = "proj:{}".format(ptag)
            active_hours = (r["active_minutes_total"] or 0) / 60.0
            # log-ish growth: 0h → 4, 1h → 5, 10h → 7, 100h → 9, 1000h → 11
            size_hint = max(4, int(4 + (active_hours ** 0.4)))
            tags = []
            if r["treat_as"] and r["treat_as"] != "auto":
                tags.append(r["treat_as"])
            last_iso = r["last_active_at"] or ""
            if last_iso and last_iso[:10] < cutoff_60d:
                tags.append("dormant")
            add_node({
                "id": pid, "type": "project",
                "label": ptag[:80],
                "confidence": "high" if active_hours >= 1 else "med",
                "size_hint": size_hint,
                "last_seen": last_iso or None,
                "tags": tags,
                "metadata": {
                    "treat_as": r["treat_as"],
                    "active_minutes": r["active_minutes_total"],
                    "session_count": r["session_count"],
                    "has_narrative": bool(r["narrative"]),
                },
            })

        # ── Edges ──────────────────────────────────────────────
        # 1) evidence_for_question rows → (evidence → question) edges
        ev_rows = self._conn.execute(
            "SELECT question_id, evidence_table, evidence_id, "
            "       contribution, reason "
            "FROM evidence_for_question"
        ).fetchall()
        # token-prefix map for evidence tables
        TBL_PREFIX = {
            "summaries_gist": "g",
            "summaries_episode": "e",
            "summaries_theme": "t",
            "patterns": "p",
            "drift_observations": "d",
        }
        for ev in ev_rows:
            pfx = TBL_PREFIX.get(ev["evidence_table"])
            if not pfx:
                continue
            qid = "q:{}".format(ev["question_id"])
            evid = "{}:{}".format(pfx, ev["evidence_id"])
            add_edge(evid, qid,
                     kind="evidence",
                     contribution=ev["contribution"] or "supports",
                     label=ev["contribution"] or "supports")

        # 2) pattern.raw_pointer_id → gist (derived_from)
        pat_links = self._conn.execute(
            "SELECT id, raw_pointer_id FROM patterns "
            "WHERE raw_pointer_id IS NOT NULL"
        ).fetchall()
        for p in pat_links:
            add_edge("p:{}".format(p["id"]),
                     "g:{}".format(p["raw_pointer_id"]),
                     kind="derived_from", label="from")

        # 3) drift.raw_pointer_id → gist (derived_from)
        drift_links = self._conn.execute(
            "SELECT id, raw_pointer_id FROM drift_observations "
            "WHERE raw_pointer_id IS NOT NULL"
        ).fetchall()
        for d in drift_links:
            add_edge("d:{}".format(d["id"]),
                     "g:{}".format(d["raw_pointer_id"]),
                     kind="derived_from", label="from")

        # 4) gist → project via project: tags
        proj_tag_rows = self._conn.execute(
            "SELECT row_id, tag FROM tags "
            "WHERE table_name = 'summaries_gist' "
            "  AND tag LIKE 'project:%'"
        ).fetchall()
        for r in proj_tag_rows:
            ptag = r["tag"][len("project:"):].strip()
            if not ptag:
                continue
            add_edge("g:{}".format(r["row_id"]),
                     "proj:{}".format(ptag),
                     kind="in_project", label="in")

        stats = {
            "nodes_total": len(nodes),
            "edges_total": len(edges),
            "by_type": {},
        }
        for n in nodes:
            stats["by_type"][n["type"]] = stats["by_type"].get(
                n["type"], 0) + 1

        return {"nodes": nodes, "edges": edges, "stats": stats}

    def list_evidence_for_question(self, question_id, *, limit=50):
        """Returns evidence rows. Optional join to gist body etc. is
        the caller's job — keep this query schema-agnostic."""
        rows = self._conn.execute(
            "SELECT * FROM evidence_for_question WHERE question_id = ? "
            "ORDER BY contributed_at DESC LIMIT ?",
            (int(question_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def evidence_for_artifact(self, evidence_table, evidence_id):
        """All questions this artifact has been filed against."""
        rows = self._conn.execute(
            "SELECT e.*, q.question, q.lifecycle "
            "FROM evidence_for_question e "
            "JOIN open_questions q ON q.id = e.question_id "
            "WHERE e.evidence_table = ? AND e.evidence_id = ?",
            (evidence_table, int(evidence_id)),
        ).fetchall()
        return [dict(r) for r in rows]

    def question_with_evidence(self, question_id, *, recent_n=5):
        """Question + decorated recent evidence (with gist bodies pulled
        in for the common case)."""
        q = self.get_question(question_id)
        if not q:
            return None
        ev_rows = self.list_evidence_for_question(question_id, limit=recent_n)
        # Decorate gist evidence with body text (the most common type)
        out_evidence = []
        for ev in ev_rows:
            decorated = dict(ev)
            if ev["evidence_table"] == "summaries_gist":
                gist = self._conn.execute(
                    "SELECT body, period_label, confidence, created_at "
                    "FROM summaries_gist WHERE id = ?",
                    (ev["evidence_id"],)
                ).fetchone()
                if gist:
                    decorated["evidence_body"] = gist["body"]
                    decorated["evidence_label"] = gist["period_label"]
                    decorated["evidence_confidence"] = gist["confidence"]
                    decorated["evidence_created_at"] = gist["created_at"]
            out_evidence.append(decorated)
        q["tags"] = self.get_tags_for("open_questions", q["id"])
        q["recent_evidence"] = out_evidence
        return q

    def top_questions_with_evidence(self, *, limit=10, recent_n=3):
        """Working-memory-ready: active questions + recent evidence each.

        Per locked design (3f.5/#2): this is the new PRIMARY view of
        the user's standing concerns. Working memory builds around this.
        """
        questions = self.active_questions(limit=limit)
        out = []
        for q in questions:
            decorated = self.question_with_evidence(
                q["id"], recent_n=recent_n)
            if decorated:
                out.append(decorated)
        return out

    def unfiled_recent_gists(self, *, limit=20):
        """Recent gists that haven't been routed to any question.
        Surfaced in working memory so the user can see what didn't fit
        the existing questions — sometimes that's the signal of a new
        question forming."""
        rows = self._conn.execute(
            "SELECT g.* FROM summaries_gist g "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM evidence_for_question e "
            "  WHERE e.evidence_table = 'summaries_gist' "
            "  AND e.evidence_id = g.id"
            ") ORDER BY g.id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── patterns ────────────────────────────────────────────────

    def add_pattern(self, name, body, *, confidence="med",
                    raw_pointer_id=None, tags=None, occurrences=1):
        cur = self._conn.execute(
            "INSERT INTO patterns (name, body, confidence, raw_pointer_id, "
            "occurrences) VALUES (?, ?, ?, ?, ?)",
            (name, body, _norm_confidence(confidence),
             raw_pointer_id, occurrences),
        )
        self._safe_commit()
        pid = cur.lastrowid
        self.tag_many("patterns", pid, tags)
        return pid

    def recent_patterns(self, limit=20):
        rows = self._conn.execute(
            "SELECT * FROM patterns ORDER BY last_observed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pattern(self, pattern_id):
        row = self._conn.execute(
            "SELECT * FROM patterns WHERE id = ?", (int(pattern_id),),
        ).fetchone()
        return dict(row) if row else None

    # ── drift_observations ──────────────────────────────────────

    def add_drift(self, body, *, direction="", confidence="med",
                  raw_pointer_id=None, tags=None):
        cur = self._conn.execute(
            "INSERT INTO drift_observations (body, direction, confidence, "
            "raw_pointer_id) VALUES (?, ?, ?, ?)",
            (body, direction, _norm_confidence(confidence), raw_pointer_id),
        )
        self._safe_commit()
        did = cur.lastrowid
        self.tag_many("drift_observations", did, tags)
        return did

    def recent_drift(self, limit=20):
        rows = self._conn.execute(
            "SELECT * FROM drift_observations ORDER BY observed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_drift(self, drift_id):
        row = self._conn.execute(
            "SELECT * FROM drift_observations WHERE id = ?", (int(drift_id),),
        ).fetchone()
        return dict(row) if row else None

    # ── future_overseer_notes (append-only) ─────────────────────

    def append_future_note(self, instance_id, body, consolidation_id=None):
        """Append a note to the institutional memory. NEVER updates or deletes."""
        cur = self._conn.execute(
            "INSERT INTO future_overseer_notes (instance_id, body, "
            "consolidation_id) VALUES (?, ?, ?)",
            (instance_id, body, consolidation_id),
        )
        self._safe_commit()
        return cur.lastrowid

    def all_future_notes(self):
        """All notes, oldest first — read-as-accreted."""
        rows = self._conn.execute(
            "SELECT * FROM future_overseer_notes ORDER BY written_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_future_note(self, note_id):
        row = self._conn.execute(
            "SELECT * FROM future_overseer_notes WHERE id = ?",
            (int(note_id),),
        ).fetchone()
        return dict(row) if row else None

    # ── llm_calls ───────────────────────────────────────────────

    def log_llm_call(self, *, requested_backend, actual_backend, model="",
                     prompt_chars=0, response_chars=0,
                     prompt_tokens=0, response_tokens=0,
                     latency_ms=0, cost_usd=0.0, degraded=False,
                     ok=True, error="", purpose=""):
        cur = self._conn.execute(
            "INSERT INTO llm_calls (requested_backend, actual_backend, model, "
            "prompt_chars, response_chars, prompt_tokens, response_tokens, "
            "latency_ms, cost_usd, degraded, ok, error, purpose) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (requested_backend, actual_backend, model,
             prompt_chars, response_chars, prompt_tokens, response_tokens,
             latency_ms, cost_usd, 1 if degraded else 0,
             1 if ok else 0, error, purpose),
        )
        self._safe_commit()
        return cur.lastrowid

    def llm_call_stats(self, days=7):
        rows = self._conn.execute(
            "SELECT actual_backend, COUNT(*) AS calls, "
            "SUM(ok) AS oks, "
            "SUM(degraded) AS degraded_calls, "
            "ROUND(AVG(latency_ms)) AS avg_ms, "
            "ROUND(SUM(cost_usd), 4) AS total_cost_usd "
            "FROM llm_calls "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY actual_backend ORDER BY calls DESC",
            ("-{} days".format(days),),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_llm_calls(self, limit=20):
        rows = self._conn.execute(
            "SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def llm_attribution_stats(self, days=7):
        """Slice 14.6 CP1: per-model + per-purpose breakdown of
        LLM spend. Lets us see which model did how much work for
        which task type at what cost — the data we need to decide
        whether a routing choice is paying off.

        Returns three tables (each a list of dicts):
          - by_model_purpose: rows of (model, purpose, calls, ok,
            total_cost_usd, avg_cost_usd, avg_latency_ms,
            total_prompt_tokens, total_response_tokens)
          - by_purpose: rolled up across models — total spend per task
          - by_model: rolled up across purposes — total spend per model
        """
        days_str = "-{} days".format(int(days))
        # Per model+purpose — Slice 14.7 adds avg input/output tokens
        # so we can see token-mix per task type, not just $ aggregates.
        rows = self._conn.execute(
            "SELECT model, COALESCE(NULLIF(purpose,''),'(unspecified)') "
            "    AS purpose, "
            "  COUNT(*) AS calls, "
            "  SUM(ok) AS oks, "
            "  ROUND(SUM(cost_usd), 4) AS total_cost_usd, "
            "  ROUND(AVG(cost_usd), 6) AS avg_cost_usd, "
            "  ROUND(AVG(latency_ms)) AS avg_latency_ms, "
            "  SUM(prompt_tokens) AS total_prompt_tokens, "
            "  SUM(response_tokens) AS total_response_tokens, "
            "  ROUND(AVG(prompt_tokens), 1) AS avg_prompt_tokens, "
            "  ROUND(AVG(response_tokens), 1) AS avg_response_tokens "
            "FROM llm_calls "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY model, purpose "
            "ORDER BY total_cost_usd DESC",
            (days_str,),
        ).fetchall()
        by_model_purpose = [dict(r) for r in rows]
        # Rolled up by purpose
        rows = self._conn.execute(
            "SELECT COALESCE(NULLIF(purpose,''),'(unspecified)') AS purpose, "
            "  COUNT(*) AS calls, "
            "  ROUND(SUM(cost_usd), 4) AS total_cost_usd, "
            "  ROUND(AVG(cost_usd), 6) AS avg_cost_usd "
            "FROM llm_calls "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY purpose ORDER BY total_cost_usd DESC",
            (days_str,),
        ).fetchall()
        by_purpose = [dict(r) for r in rows]
        # Rolled up by model
        rows = self._conn.execute(
            "SELECT model, COUNT(*) AS calls, "
            "  ROUND(SUM(cost_usd), 4) AS total_cost_usd, "
            "  ROUND(AVG(cost_usd), 6) AS avg_cost_usd "
            "FROM llm_calls "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY model ORDER BY total_cost_usd DESC",
            (days_str,),
        ).fetchall()
        by_model = [dict(r) for r in rows]
        # Slice 14.7: by-layer rollup. Buckets every purpose into one
        # of four layers so the daily dashboard shows where the spend
        # actually lives — without the user having to read every
        # purpose name.
        LAYER_MAP = {
            "router-chat":          "router",
            "overseer-chat":        "overseer",
            "overseer-journal":     "overseer",
            "summarize-session":    "routine",
            "summarize-recent":     "routine",
            "working-memory":       "routine",
            "auto-tag-notes":       "routine",
            "evidence-routing":     "routine",
            "insight-scan":         "routine",
            "distill-corrections":  "routine",
            "project-narrative":    "routine",
            "temporal-daily":       "routine",
            "temporal-weekly":      "routine",
            "temporal-monthly":     "routine",
            "temporal-yearly":      "routine",
            "dialectic-check":      "dialectic",
            "chat-compress":        "overseer",
        }
        by_layer_acc: dict = {}
        for r in by_purpose:
            layer = LAYER_MAP.get(r["purpose"], "other")
            acc = by_layer_acc.setdefault(
                layer, {"layer": layer, "calls": 0,
                        "total_cost_usd": 0.0})
            acc["calls"] += int(r["calls"] or 0)
            acc["total_cost_usd"] += float(r["total_cost_usd"] or 0)
        total_cost = sum(
            v["total_cost_usd"] for v in by_layer_acc.values()) or 1.0
        by_layer = []
        for layer in ("router", "overseer", "routine", "dialectic",
                       "other"):
            v = by_layer_acc.get(layer)
            if not v:
                continue
            v["total_cost_usd"] = round(v["total_cost_usd"], 4)
            v["pct_of_spend"] = round(
                100.0 * v["total_cost_usd"] / total_cost, 1)
            by_layer.append(v)

        return {
            "days": int(days),
            "by_model_purpose": by_model_purpose,
            "by_purpose": by_purpose,
            "by_model": by_model,
            "by_layer": by_layer,
            "total_cost_usd": round(total_cost, 4),
        }

    # ── processed_sessions / processed_notes (loop idempotency) ─

    def is_session_processed(self, session_id):
        row = self._conn.execute(
            "SELECT 1 FROM processed_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row is not None

    def mark_session_processed(self, session_id, *, gist_id=None,
                               episode_id=None, notes_count=0, error=""):
        """Idempotent: re-marking the same session_id replaces the row."""
        self._conn.execute(
            "INSERT INTO processed_sessions (session_id, gist_id, episode_id, "
            "notes_count, error) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "processed_at=datetime('now'), gist_id=excluded.gist_id, "
            "episode_id=excluded.episode_id, notes_count=excluded.notes_count, "
            "error=excluded.error",
            (session_id, gist_id, episode_id, notes_count, error or ""),
        )
        self._safe_commit()

    def is_note_processed(self, note_id):
        row = self._conn.execute(
            "SELECT 1 FROM processed_notes WHERE note_id = ?", (note_id,),
        ).fetchone()
        return row is not None

    def mark_note_processed(self, note_id, *, tags_added="", error=""):
        self._conn.execute(
            "INSERT INTO processed_notes (note_id, tags_added, error) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(note_id) DO UPDATE SET "
            "processed_at=datetime('now'), tags_added=excluded.tags_added, "
            "error=excluded.error",
            (int(note_id), tags_added or "", error or ""),
        )
        self._safe_commit()

    def processed_session_count(self):
        return self._conn.execute(
            "SELECT COUNT(*) FROM processed_sessions"
        ).fetchone()[0]

    def processed_note_count(self):
        return self._conn.execute(
            "SELECT COUNT(*) FROM processed_notes"
        ).fetchone()[0]

    # ── imported_sessions (slice 3d) ────────────────────────────

    def add_imported_session(self, *, id, source, source_path, project="",
                             cwd="", git_branch="", started_at=None,
                             ended_at=None, duration_minutes=0,
                             message_count=0, user_message_count=0,
                             assistant_message_count=0, tool_use_count=0,
                             bytes_size=0, file_hash="", metadata_json="{}"):
        """Insert an imported_sessions row. Idempotent on `id` —
        re-inserting the same id replaces the metadata. Dedup by content
        hash is enforced separately via UNIQUE(source, file_hash) — call
        get_imported_by_hash() first if you want to skip duplicates.
        """
        self._conn.execute(
            "INSERT INTO imported_sessions (id, source, source_path, "
            "project, cwd, git_branch, started_at, ended_at, "
            "duration_minutes, message_count, user_message_count, "
            "assistant_message_count, tool_use_count, bytes_size, "
            "file_hash, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "source_path=excluded.source_path, project=excluded.project, "
            "cwd=excluded.cwd, git_branch=excluded.git_branch, "
            "started_at=excluded.started_at, ended_at=excluded.ended_at, "
            "duration_minutes=excluded.duration_minutes, "
            "message_count=excluded.message_count, "
            "user_message_count=excluded.user_message_count, "
            "assistant_message_count=excluded.assistant_message_count, "
            "tool_use_count=excluded.tool_use_count, "
            "bytes_size=excluded.bytes_size, file_hash=excluded.file_hash, "
            "metadata_json=excluded.metadata_json",
            (id, source, source_path, project, cwd, git_branch,
             started_at, ended_at, duration_minutes,
             message_count, user_message_count,
             assistant_message_count, tool_use_count, bytes_size,
             file_hash, metadata_json),
        )
        self._safe_commit()
        return id

    def get_imported_by_id(self, imported_id):
        row = self._conn.execute(
            "SELECT * FROM imported_sessions WHERE id = ?", (imported_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_imported_by_hash(self, source, file_hash):
        if not file_hash:
            return None
        row = self._conn.execute(
            "SELECT * FROM imported_sessions WHERE source = ? AND file_hash = ?",
            (source, file_hash),
        ).fetchone()
        return dict(row) if row else None

    def list_imported_sessions(self, *, source=None, limit=200, offset=0):
        sql = "SELECT * FROM imported_sessions"
        params: list = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        sql += " ORDER BY started_at DESC NULLS LAST, imported_at DESC " \
               "LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Older SQLite (< 3.30) doesn't support NULLS LAST. Retry without.
            sql = sql.replace(" NULLS LAST", "")
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def list_unprocessed_imported_sessions(self, *, source=None, limit=200):
        """Return unprocessed imported_sessions only — SQL-level filter.

        The loop's _summarize_imported_sessions used to call
        list_imported_sessions(limit=200) and then filter in Python.
        That starved the 1,129-row historical backlog (Slice 9.1
        grok-com / tweets), because the top-200-by-started_at window
        was fully covered by already-processed recent imports — the
        Python filter saw zero unprocessed rows and bailed.

        This method does the filter at the SQL layer (LEFT JOIN +
        WHERE processed.imported_id IS NULL) and orders by
        imported_at DESC so freshly-pushed imports still get priority,
        with the historical backlog draining behind them.

        Returns up to `limit` rows. Returned dicts match the
        imported_sessions schema (same shape as list_imported_sessions).
        """
        sql = (
            "SELECT i.* FROM imported_sessions i "
            "LEFT JOIN processed_imported_sessions p "
            "  ON p.imported_id = i.id "
            "WHERE p.imported_id IS NULL"
        )
        params: list = []
        if source:
            sql += " AND i.source = ?"
            params.append(source)
        sql += " ORDER BY i.imported_at DESC, i.started_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def delete_imported_session(self, imported_id):
        self._conn.execute(
            "DELETE FROM processed_imported_sessions WHERE imported_id = ?",
            (imported_id,),
        )
        cur = self._conn.execute(
            "DELETE FROM imported_sessions WHERE id = ?", (imported_id,)
        )
        self._safe_commit()
        return cur.rowcount

    # ── Slice 9.8 (2026-05-20): imported session redaction ─────

    REDACTED_PLACEHOLDER_LINE = (
        '{"type":"user","message":{"role":"user",'
        '"content":"[REDACTED]"},"_redacted":true}\n'
    )

    def redact_imported_session(self, imported_id, *, mode="mark_redacted"):
        """Redact an imported session in one of two modes:

          mark_redacted (default, recoverable-via-backup-only):
            - Overwrites the on-disk .jsonl with a single [REDACTED]
              placeholder line so subsequent reads return harmless
              content but the file still exists.
            - Sets redacted_at on the row.
            - Keeps metadata (timestamps, project, source) so session
              counts + project_summaries don't lie.
            - Sets bytes_size to the placeholder length, file_hash to
              the new hash, so downstream code sees consistent state.
            - If the row was processed_imported_sessions, that record
              is preserved (the gist is independent).

          delete_row (destructive):
            - Deletes the .jsonl file from disk.
            - Removes the imported_sessions row + any
              processed_imported_sessions record.

        Returns dict {ok, mode, imported_id, path, action}.
        """
        import os, hashlib
        from pathlib import Path

        row = self._conn.execute(
            "SELECT id, source_path FROM imported_sessions WHERE id = ?",
            (imported_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "imported_session not found"}
        sp = row[1] or ""

        if mode == "delete_row":
            # Remove file from disk first (best-effort), then DB rows.
            deleted_file = False
            if sp and Path(sp).is_file():
                try:
                    os.remove(sp)
                    deleted_file = True
                except Exception as e:
                    log.warning("delete_row: file remove failed: %s", e)
            self._conn.execute(
                "DELETE FROM processed_imported_sessions "
                "WHERE imported_id = ?", (imported_id,),
            )
            cur = self._conn.execute(
                "DELETE FROM imported_sessions WHERE id = ?",
                (imported_id,),
            )
            self._safe_commit()
            return {
                "ok": True, "mode": "delete_row",
                "imported_id": imported_id, "path": sp,
                "row_deleted": cur.rowcount > 0,
                "file_deleted": deleted_file,
            }

        # default: mark_redacted
        if mode != "mark_redacted":
            return {"ok": False, "error": f"unknown mode: {mode}"}

        placeholder = self.REDACTED_PLACEHOLDER_LINE.encode("utf-8")
        wrote_file = False
        if sp:
            try:
                Path(sp).parent.mkdir(parents=True, exist_ok=True)
                Path(sp).write_bytes(placeholder)
                wrote_file = True
            except Exception as e:
                log.warning("mark_redacted: file overwrite failed: %s", e)

        new_hash = hashlib.sha256(placeholder).hexdigest()
        new_size = len(placeholder)
        self._conn.execute(
            "UPDATE imported_sessions "
            "SET redacted_at = datetime('now'), "
            "    bytes_size = ?, file_hash = ?, "
            "    message_count = 1, user_message_count = 1, "
            "    assistant_message_count = 0, tool_use_count = 0 "
            "WHERE id = ?",
            (new_size, new_hash, imported_id),
        )
        self._safe_commit()
        return {
            "ok": True, "mode": "mark_redacted",
            "imported_id": imported_id, "path": sp,
            "file_overwritten": wrote_file,
            "new_bytes_size": new_size,
        }

    # ── Slice 9.8 (2026-05-20): sensitivity scan ───────────────

    # Hardcoded default sensitive-content regexes. Conservative — we
    # want true positives to dominate so Tory's review queue stays
    # short. Custom patterns can be passed by overseer in the scan
    # call for project-specific things (Tory's address, names of
    # people he wants kept private, internal API endpoints, etc.).
    DEFAULT_SENSITIVE_PATTERNS = [
        # name              regex pattern                                     description
        ("openai_key",      r"sk-[A-Za-z0-9]{20,}",                            "OpenAI-style API key"),
        ("anthropic_key",   r"sk-ant-[A-Za-z0-9_-]{20,}",                      "Anthropic API key"),
        ("github_pat",      r"(github_pat_|ghp_)[A-Za-z0-9_]{20,}",            "GitHub Personal Access Token"),
        ("aws_key",         r"AKIA[0-9A-Z]{16}",                               "AWS Access Key ID"),
        ("stripe_secret",   r"sk_(live|test)_[A-Za-z0-9]{24,}",                "Stripe secret key"),
        ("slack_token",     r"xox[baprs]-[A-Za-z0-9-]{10,}",                   "Slack token"),
        ("bearer_token",    r"[Bb]earer\s+[A-Za-z0-9._-]{20,}",                "HTTP Bearer token"),
        ("private_key",     r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----",        "PEM private key block"),
        ("ssh_key",         r"ssh-(?:rsa|ed25519|dss) [A-Za-z0-9+/=]{60,}",    "SSH public key (often paired with private)"),
        # Tightened to require proper 4-4-4-4 separators or a contiguous
        # 16-digit run — the original \b(?:\d[ -]?){13,16}\b matched
        # timestamps and session IDs ("332 2026-05-17 23"). False
        # negatives on 15-digit Amex / 14-digit Diners are acceptable
        # given the noise reduction.
        ("credit_card",     r"\b(?:\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}|\d{16})\b", "Possible credit-card number (4-4-4-4 or 16 contiguous digits)"),
        ("ssn",             r"\b\d{3}-\d{2}-\d{4}\b",                           "US Social Security Number pattern"),
        # Tightened: require at least one separator between number
        # groups so we don't match raw 10-digit session IDs.
        ("us_phone",        r"\b(?:\+?1[-.\s])?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}\b", "US phone number (formatted)"),
        ("password_assign", r"(?i)(?:password|passwd|pwd|api[_-]?key|secret)\s*[:=]\s*[\"']?[A-Za-z0-9!@#$%^&*_-]{6,}",
                            "Inline password/secret assignment"),
        ("jwt",             r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
                            "JSON Web Token"),
    ]

    def scan_imported_session_for_sensitive(self, imported_id, *,
                                              extra_patterns=None,
                                              use_defaults=True,
                                              max_matches=20):
        """Scan one imported_session's on-disk content for sensitive
        regex matches. Returns dict with the matches found.

        extra_patterns: optional list of (name, regex_str,
            description) tuples added on top of (or in place of when
            use_defaults=False) DEFAULT_SENSITIVE_PATTERNS.

        Each match: {pattern_name, description, snippet, char_offset,
            line_no}.  snippet is the surrounding ±60 chars, with the
            matched text intact (so Tory can verify before redacting).
        """
        import re
        from pathlib import Path

        row = self._conn.execute(
            "SELECT id, source_path, source, project, redacted_at "
            "FROM imported_sessions WHERE id = ?", (imported_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "imported_session not found"}
        if row[4]:  # already redacted
            return {
                "ok": True, "imported_id": imported_id,
                "already_redacted": True, "matches": [],
            }
        sp = row[1]
        if not sp or not Path(sp).is_file():
            return {
                "ok": False, "error": f"source_path missing or not a file: {sp}",
            }

        try:
            content = Path(sp).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "error": f"read failed: {e}"[:200]}

        patterns = list(self.DEFAULT_SENSITIVE_PATTERNS) if use_defaults else []
        if extra_patterns:
            for ep in extra_patterns:
                if isinstance(ep, (list, tuple)) and len(ep) >= 2:
                    name = ep[0]
                    pat = ep[1]
                    desc = ep[2] if len(ep) >= 3 else "custom pattern"
                    patterns.append((name, pat, desc))

        matches = []
        for name, pat, desc in patterns:
            try:
                rx = re.compile(pat)
            except re.error:
                continue
            for m in rx.finditer(content):
                start = max(0, m.start() - 60)
                end = min(len(content), m.end() + 60)
                snippet = content[start:end].replace("\n", " ")
                line_no = content.count("\n", 0, m.start()) + 1
                matches.append({
                    "pattern_name": name,
                    "description": desc,
                    "snippet": snippet,
                    "match_text_preview": m.group(0)[:40],
                    "char_offset": m.start(),
                    "line_no": line_no,
                })
                if len(matches) >= max_matches:
                    break
            if len(matches) >= max_matches:
                break

        return {
            "ok": True,
            "imported_id": imported_id,
            "source": row[2],
            "project": row[3],
            "match_count": len(matches),
            "matches": matches,
            "patterns_run": len(patterns),
        }

    def scan_imported_sessions_batch(self, *, source=None, since=None,
                                       limit=20, extra_patterns=None,
                                       use_defaults=True):
        """Scan up to `limit` imported_sessions (newest first, optionally
        filtered by source + since). Skip already-redacted rows. Returns
        a list of dicts (one per scanned session) with match counts."""
        sql = ("SELECT id FROM imported_sessions "
               "WHERE redacted_at IS NULL")
        params = []
        if source:
            sql += " AND source = ?"
            params.append(source)
        if since:
            sql += " AND started_at >= ?"
            params.append(since)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        ids = [r[0] for r in self._conn.execute(sql, params).fetchall()]

        results = []
        for iid in ids:
            res = self.scan_imported_session_for_sensitive(
                iid, extra_patterns=extra_patterns,
                use_defaults=use_defaults,
            )
            if res.get("ok") and res.get("match_count", 0) > 0:
                results.append({
                    "imported_id": iid,
                    "match_count": res["match_count"],
                    "source": res.get("source", ""),
                    "project": res.get("project", ""),
                    "matches": res["matches"],
                })
        return {
            "ok": True,
            "scanned": len(ids),
            "with_matches": len(results),
            "results": results,
        }

    def is_imported_processed(self, imported_id):
        row = self._conn.execute(
            "SELECT 1 FROM processed_imported_sessions WHERE imported_id = ?",
            (imported_id,),
        ).fetchone()
        return row is not None

    def mark_imported_processed(self, imported_id, *, gist_id=None,
                                notes_used=0, error=""):
        self._conn.execute(
            "INSERT INTO processed_imported_sessions (imported_id, "
            "gist_id, notes_used, error) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(imported_id) DO UPDATE SET "
            "processed_at=datetime('now'), gist_id=excluded.gist_id, "
            "notes_used=excluded.notes_used, error=excluded.error",
            (imported_id, gist_id, int(notes_used), error or ""),
        )
        self._safe_commit()

    # ── Slice 3e: project classification ────────────────────────

    AUTO_CLASSIFY_MIN_COUNT = 10            # need at least N imports
    AUTO_CLASSIFY_MAX_MEDIAN_MIN = 2.0      # median duration < N min → auto

    def get_project_setting(self, project):
        """Return the per-project setting row, or a synthesized 'auto' row
        if no record exists yet."""
        row = self._conn.execute(
            "SELECT * FROM imported_project_settings WHERE project = ?",
            (project,),
        ).fetchone()
        if row:
            return dict(row)
        return {
            "project": project, "treat_as": "auto",
            "classified_at": None, "classified_reason": "",
            "manual_override": 0,
        }

    def list_project_settings(self):
        rows = self._conn.execute(
            "SELECT * FROM imported_project_settings ORDER BY project"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_project_setting(self, project, *, treat_as, manual_override,
                            classified_reason=""):
        """Upsert a per-project setting. manual_override=1 prevents the
        auto-classifier from changing this row."""
        valid = ("auto", "human", "automation", "ignore")
        if treat_as not in valid:
            raise ValueError(
                "treat_as must be one of {}".format(valid))
        self._conn.execute(
            "INSERT INTO imported_project_settings (project, treat_as, "
            "classified_at, classified_reason, manual_override, updated_at) "
            "VALUES (?, ?, datetime('now'), ?, ?, datetime('now')) "
            "ON CONFLICT(project) DO UPDATE SET "
            "treat_as=excluded.treat_as, "
            "classified_at=CASE WHEN excluded.manual_override=1 "
            "                   THEN imported_project_settings.classified_at "
            "                   ELSE excluded.classified_at END, "
            "classified_reason=excluded.classified_reason, "
            "manual_override=excluded.manual_override, "
            "updated_at=datetime('now')",
            (project, treat_as, classified_reason,
             1 if manual_override else 0),
        )
        self._safe_commit()

    def auto_classify_projects(self):
        """Run the automation heuristic over all projects in
        imported_sessions. Returns a list of changes (or no-ops).

        Heuristic uses the MEDIAN duration, not the mean — UFOSINT-class
        automations pile up many short runs but a few long sessions
        get mixed in (manual debug runs). Mean is dragged toward those
        outliers; median tracks the typical run.

        A project is 'automation' if:
          - count >= AUTO_CLASSIFY_MIN_COUNT (default 10), AND
          - median duration < AUTO_CLASSIFY_MAX_MEDIAN_MIN (default 2)

        Otherwise 'human'. Skips projects where manual_override=1.
        """
        import statistics

        # Pull all (project, duration, msgs) — group in Python so we can
        # compute median.
        rows = list(self._conn.execute(
            "SELECT project, duration_minutes, message_count "
            "FROM imported_sessions"
        ))
        from collections import defaultdict
        groups: dict[str, list[tuple]] = defaultdict(list)
        for r in rows:
            groups[r["project"] or ""].append(
                (r["duration_minutes"] or 0, r["message_count"] or 0))

        out = []
        for project, sessions in groups.items():
            n = len(sessions)
            durations = [s[0] for s in sessions]
            msgs = [s[1] for s in sessions]
            median_min = (statistics.median(durations)
                          if durations else 0.0)
            mean_min = sum(durations) / n if n else 0.0
            mean_msg = sum(msgs) / n if n else 0.0
            median_msg = (statistics.median(msgs) if msgs else 0)

            existing = self.get_project_setting(project)
            if existing.get("manual_override"):
                out.append({
                    "project": project, "skipped": "manual_override",
                    "treat_as": existing["treat_as"],
                    "n": n,
                })
                continue

            is_automation = (
                n >= self.AUTO_CLASSIFY_MIN_COUNT
                and median_min < self.AUTO_CLASSIFY_MAX_MEDIAN_MIN
            )
            new_treat = "automation" if is_automation else "human"
            reason = (
                "{n} sessions, median {med:.1f}min "
                "(mean {mean:.1f}m), median {medm} msgs "
                "(mean {meanm:.1f})"
            ).format(
                n=n, med=median_min, mean=mean_min,
                medm=int(median_msg), meanm=mean_msg,
            )
            if existing.get("treat_as") != new_treat:
                self.set_project_setting(
                    project, treat_as=new_treat,
                    manual_override=False,
                    classified_reason=reason)
                out.append({"project": project, "changed_to": new_treat,
                            "reason": reason, "n": n,
                            "median_minutes": round(median_min, 2)})
            else:
                self.set_project_setting(
                    project, treat_as=new_treat,
                    manual_override=False,
                    classified_reason=reason)
                out.append({"project": project, "unchanged": new_treat,
                            "reason": reason, "n": n,
                            "median_minutes": round(median_min, 2)})
        return out

    # ── Slice 3e: automation rollups ────────────────────────────

    def get_rollup(self, project, rollup_date):
        row = self._conn.execute(
            "SELECT * FROM automation_rollups "
            "WHERE project = ? AND rollup_date = ?",
            (project, rollup_date),
        ).fetchone()
        return dict(row) if row else None

    def upsert_rollup(self, *, project, rollup_date, session_count,
                      total_messages, total_minutes, error_signals,
                      median_minutes, max_minutes, summary,
                      gist_id=None, sample_session_ids=None):
        sample = json.dumps(sample_session_ids or [])
        self._conn.execute(
            "INSERT INTO automation_rollups (project, rollup_date, "
            "session_count, total_messages, total_minutes, error_signals, "
            "median_minutes, max_minutes, summary, gist_id, "
            "sample_session_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project, rollup_date) DO UPDATE SET "
            "session_count=excluded.session_count, "
            "total_messages=excluded.total_messages, "
            "total_minutes=excluded.total_minutes, "
            "error_signals=excluded.error_signals, "
            "median_minutes=excluded.median_minutes, "
            "max_minutes=excluded.max_minutes, "
            "summary=excluded.summary, gist_id=excluded.gist_id, "
            "sample_session_ids=excluded.sample_session_ids",
            (project, rollup_date, session_count, total_messages,
             total_minutes, error_signals, median_minutes, max_minutes,
             summary, gist_id, sample),
        )
        self._safe_commit()

    def list_rollups(self, *, project=None, limit=200):
        sql = "SELECT * FROM automation_rollups"
        params = []
        if project:
            sql += " WHERE project = ?"
            params.append(project)
        sql += " ORDER BY rollup_date DESC, project ASC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_rollup_by_id(self, rollup_id):
        row = self._conn.execute(
            "SELECT * FROM automation_rollups WHERE id = ?",
            (int(rollup_id),),
        ).fetchone()
        return dict(row) if row else None

    def imports_for_rollup(self, project, rollup_date):
        """All imports for a project on a given UTC date."""
        date_start = rollup_date + "T00:00:00"
        date_end = rollup_date + "T23:59:59"
        rows = self._conn.execute(
            "SELECT * FROM imported_sessions WHERE project = ? "
            "AND ((started_at >= ? AND started_at <= ?) "
            "  OR (ended_at >= ? AND ended_at <= ?)) "
            "ORDER BY started_at ASC",
            (project, date_start, date_end, date_start, date_end),
        ).fetchall()
        return [dict(r) for r in rows]

    def imports_dates_for_project(self, project):
        """Distinct UTC dates with imports for a project."""
        rows = self._conn.execute(
            "SELECT DISTINCT substr(COALESCE(started_at, ended_at), 1, 10) "
            "  AS d FROM imported_sessions "
            "WHERE project = ? AND COALESCE(started_at, ended_at) IS NOT NULL "
            "ORDER BY d ASC",
            (project,),
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    # ── Slice 3e: chat ──────────────────────────────────────────
    # Agent harness (2026-07-10): every helper takes an optional
    # thread_id. None means "the active thread" (overseer_state key
    # chat_active_thread_id) so pre-thread consumers — voice mode,
    # the MCP overseer_chat tool, the router streak counter, the
    # compress_chat tool — stay coherent without code changes.

    def active_chat_thread_id(self):
        """Resolve the active thread id, healing a stale or missing
        pointer. Creates a fresh thread if none exist."""
        raw = self.get_overseer_state("chat_active_thread_id")
        try:
            tid = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            tid = 0
        if tid:
            row = self._conn.execute(
                "SELECT id FROM chat_threads WHERE id = ?", (tid,)
            ).fetchone()
            if row:
                return tid
        # Pointer missing or dangling — fall back to the most recently
        # touched thread, else create one.
        row = self._conn.execute(
            "SELECT id FROM chat_threads "
            "ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
        if row:
            tid = row["id"]
        else:
            cur = self._conn.execute(
                "INSERT INTO chat_threads (title) VALUES ('')")
            tid = cur.lastrowid
        self.set_overseer_state("chat_active_thread_id", tid)
        self._safe_commit()
        return tid

    def _resolve_thread_id(self, thread_id):
        if thread_id is None:
            return self.active_chat_thread_id()
        return int(thread_id)

    def list_chat_threads(self):
        """All threads newest-touched first, with message counts and
        summed cost so the sidebar can show weight at a glance."""
        rows = self._conn.execute(
            "SELECT t.id, t.title, t.created_at, t.updated_at, "
            "COUNT(m.id) AS message_count, "
            "COALESCE(SUM(m.cost_usd), 0) AS cost_usd "
            "FROM chat_threads t "
            "LEFT JOIN chat_messages m ON m.thread_id = t.id "
            "GROUP BY t.id "
            "ORDER BY t.updated_at DESC, t.id DESC").fetchall()
        return [dict(r) for r in rows]

    def create_chat_thread(self, title=""):
        """New thread becomes the active one (matches the UI flow:
        'New chat' switches you into it)."""
        cur = self._conn.execute(
            "INSERT INTO chat_threads (title) VALUES (?)",
            ((title or "").strip()[:120],))
        tid = cur.lastrowid
        self.set_overseer_state("chat_active_thread_id", tid)
        self._safe_commit()
        return tid

    def select_chat_thread(self, thread_id):
        row = self._conn.execute(
            "SELECT id FROM chat_threads WHERE id = ?",
            (int(thread_id),)).fetchone()
        if not row:
            return False
        self.set_overseer_state("chat_active_thread_id", int(thread_id))
        self._safe_commit()
        return True

    def rename_chat_thread(self, thread_id, title):
        cur = self._conn.execute(
            "UPDATE chat_threads SET title = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            ((title or "").strip()[:120], int(thread_id)))
        self._safe_commit()
        return cur.rowcount > 0

    def delete_chat_thread(self, thread_id):
        """Delete a thread + its messages + their attachment rows.
        If it was the active thread, the pointer heals to the most
        recent remaining thread (or a fresh one) on next resolve.
        try/rollback mirrors compress_chat_replace: a mid-sequence
        failure must not leave partial deletes pending for the next
        unrelated commit to silently persist."""
        tid = int(thread_id)
        with self._write_lock:
            try:
                self._conn.execute(
                    "DELETE FROM chat_message_files "
                    "WHERE chat_message_id IN "
                    "(SELECT id FROM chat_messages WHERE thread_id = ?)",
                    (tid,))
                self._conn.execute(
                    "DELETE FROM chat_messages WHERE thread_id = ?",
                    (tid,))
                cur = self._conn.execute(
                    "DELETE FROM chat_threads WHERE id = ?", (tid,))
                raw = self.get_overseer_state("chat_active_thread_id")
                if raw is not None and str(raw) == str(tid):
                    self._conn.execute(
                        "DELETE FROM overseer_state "
                        "WHERE key = 'chat_active_thread_id'")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount > 0

    def _touch_chat_thread(self, thread_id, *, role=None, content=None):
        """Bump updated_at; auto-title an untitled thread from the
        first user line (voice_chats pattern on the phone)."""
        self._conn.execute(
            "UPDATE chat_threads SET updated_at = datetime('now') "
            "WHERE id = ?", (int(thread_id),))
        if role == "user" and content:
            snippet = " ".join((content or "").split())[:60]
            if snippet:
                self._conn.execute(
                    "UPDATE chat_threads SET title = ? "
                    "WHERE id = ? AND title = ''",
                    (snippet, int(thread_id)))

    def append_chat_message(self, *, role, content, backend="", model="",
                            latency_ms=0, cost_usd=0.0,
                            prompt_tokens=0, response_tokens=0,
                            metadata=None,
                            answered_by="",
                            escalation_reason="",
                            thread_id=None):
        # Resolve + validate + insert under the write lock so a
        # concurrent delete_chat_thread can't leave this row pointing
        # at a dead thread (invisible orphan). If the requested thread
        # died mid-turn, heal to a live thread rather than orphaning.
        with self._write_lock:
            tid = self._resolve_thread_id(thread_id)
            alive = self._conn.execute(
                "SELECT 1 FROM chat_threads WHERE id = ?", (tid,)
            ).fetchone()
            if not alive:
                tid = self.active_chat_thread_id()
            cur = self._conn.execute(
                "INSERT INTO chat_messages (thread_id, role, content, "
                "backend, model, "
                "latency_ms, cost_usd, prompt_tokens, response_tokens, "
                "metadata_json, answered_by, escalation_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, role, content, backend, model, int(latency_ms),
                 float(cost_usd), int(prompt_tokens),
                 int(response_tokens),
                 json.dumps(metadata or {}),
                 answered_by, escalation_reason),
            )
            self._touch_chat_thread(tid, role=role, content=content)
            self._safe_commit()
            return cur.lastrowid

    def count_consecutive_router_turns(self, limit=8, *,
                                       thread_id=None) -> int:
        """Slice 14.7: count assistant turns at the end of the chat
        thread that were answered by the router (answered_by='router')
        without an overseer escalation breaking the streak. Used by
        the router to escalate when it's been answering on the same
        thread for too long without resolution. Per-thread: a fresh
        thread resets the streak."""
        rows = self._conn.execute(
            "SELECT role, answered_by FROM chat_messages "
            "WHERE role = 'assistant' AND thread_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (self._resolve_thread_id(thread_id), int(limit)),
        ).fetchall()
        n = 0
        for r in rows:
            if (r["answered_by"] or "") == "router":
                n += 1
            else:
                break
        return n

    def recent_chat_messages(self, limit=40, *, include_files=True,
                             thread_id=None):
        """Most-recent N rows of one thread in chronological order
        (None = active thread). When include_files is True (default),
        each row gets an `attachments` list populated from
        chat_message_files. Slice 8."""
        rows = self._conn.execute(
            "SELECT * FROM chat_messages WHERE thread_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (self._resolve_thread_id(thread_id), int(limit)),
        ).fetchall()
        msgs = list(reversed([dict(r) for r in rows]))
        if not include_files or not msgs:
            for m in msgs:
                m.setdefault("attachments", [])
            return msgs
        ids = [m["id"] for m in msgs]
        files_by_msg = self.chat_files_for_message_ids(ids)
        for m in msgs:
            m["attachments"] = files_by_msg.get(m["id"], [])
        return msgs

    def chat_message_count(self, thread_id=None):
        return self._conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE thread_id = ?",
            (self._resolve_thread_id(thread_id),),
        ).fetchone()[0]

    def clear_chat(self, thread_id=None):
        # FK ON DELETE CASCADE only fires when foreign_keys pragma is
        # ON. SQLite defaults it OFF. Delete files explicitly first so
        # a 'Clear thread' on an existing install doesn't leave orphan
        # chat_message_files rows. Scoped to one thread (None=active);
        # the thread row survives so its title/identity persist.
        tid = self._resolve_thread_id(thread_id)
        self._conn.execute(
            "DELETE FROM chat_message_files WHERE chat_message_id IN "
            "(SELECT id FROM chat_messages WHERE thread_id = ?)", (tid,))
        self._conn.execute(
            "DELETE FROM chat_messages WHERE thread_id = ?", (tid,))
        self._safe_commit()

    def compress_chat_replace(self, *, old_ids, summary_content,
                              created_at=None, metadata=None,
                              thread_id=None):
        """Slice 9.5 CP3: atomically replace a set of older chat messages
        with one synthetic 'system' role message containing their
        compressed summary.

        Order matters and we serialize under the write lock so a
        concurrent chat() write can't interleave between the delete
        and the insert.

        Returns the new chat_messages.id of the synthetic prefix row.
        """
        old_ids = [int(i) for i in (old_ids or []) if i is not None]
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._write_lock:
            # Resolve INSIDE the lock. Callers should pass the tid
            # they captured when they read the messages; a None here
            # falls back to the active pointer, which may have moved
            # during the caller's LLM call.
            tid = self._resolve_thread_id(thread_id)
            try:
                # 1. Clean up any chat_message_files belonging to the
                # messages being compressed (FK CASCADE is OFF; manual).
                if old_ids:
                    placeholders = ",".join("?" * len(old_ids))
                    self._conn.execute(
                        f"DELETE FROM chat_message_files "
                        f"WHERE chat_message_id IN ({placeholders})",
                        old_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM chat_messages "
                        f"WHERE id IN ({placeholders})",
                        old_ids,
                    )
                # 2. Insert the synthetic. created_at controls sort
                # position; we set it to the oldest dropped timestamp
                # so the prefix sorts to the HEAD of the thread.
                cur = self._conn.execute(
                    "INSERT INTO chat_messages "
                    "(thread_id, role, content, backend, model, "
                    "latency_ms, "
                    "cost_usd, prompt_tokens, response_tokens, "
                    "metadata_json, created_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tid, "system", summary_content, "compress-internal",
                     "anthropic/claude-sonnet-4.6", 0, 0.0, 0, 0,
                     meta_json,
                     created_at or datetime.now(timezone.utc).strftime(
                         "%Y-%m-%d %H:%M:%S")),
                )
                self._conn.commit()
                return cur.lastrowid
            except Exception:
                self._conn.rollback()
                raise

    # ── Agent harness (2026-07-10): prompt library ──────────────

    def list_chat_prompts(self):
        rows = self._conn.execute(
            "SELECT id, title, body, created_at, updated_at "
            "FROM chat_prompts ORDER BY title COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_chat_prompt(self, *, prompt_id=None, title, body):
        title = (title or "").strip()[:120]
        body = (body or "").strip()
        if not title or not body:
            return 0
        if prompt_id:
            cur = self._conn.execute(
                "UPDATE chat_prompts SET title = ?, body = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (title, body, int(prompt_id)))
            self._safe_commit()
            return int(prompt_id) if cur.rowcount else 0
        cur = self._conn.execute(
            "INSERT INTO chat_prompts (title, body) VALUES (?, ?)",
            (title, body))
        self._safe_commit()
        return cur.lastrowid

    def delete_chat_prompt(self, prompt_id):
        cur = self._conn.execute(
            "DELETE FROM chat_prompts WHERE id = ?", (int(prompt_id),))
        self._safe_commit()
        return cur.rowcount > 0

    # ── Agent harness (2026-07-11): interaction feedback ────────

    def add_interaction_feedback(self, *, target_kind, target_id="",
                                 rating=0, note="", context=None,
                                 source="hub"):
        # A hard string-slice would truncate mid-JSON and the discuss
        # seed would then silently drop the screen context. Fall back
        # to a minimal valid object instead.
        ctx_json = json.dumps(context or {}, ensure_ascii=False,
                              default=str)
        if len(ctx_json) > 8000:
            ctx_json = json.dumps({
                "truncated": True,
                "screen": str((context or {}).get("screen", ""))[:200],
            })
        cur = self._conn.execute(
            "INSERT INTO interaction_feedback (target_kind, target_id, "
            "rating, note, context_json, source) VALUES (?,?,?,?,?,?)",
            (target_kind, str(target_id or ""), int(rating),
             (note or "").strip()[:4000],
             ctx_json,
             str(source or "hub")[:20]))
        self._safe_commit()
        return cur.lastrowid

    def get_interaction_feedback(self, fid):
        row = self._conn.execute(
            "SELECT * FROM interaction_feedback WHERE id = ?",
            (int(fid),)).fetchone()
        return dict(row) if row else None

    def list_interaction_feedback(self, limit=50, target_kind=None):
        if target_kind:
            rows = self._conn.execute(
                "SELECT * FROM interaction_feedback "
                "WHERE target_kind = ? ORDER BY id DESC LIMIT ?",
                (target_kind, int(limit))).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM interaction_feedback "
                "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def set_feedback_thread(self, fid, thread_id):
        """Links the discuss thread. Conditional on meta_thread_id
        still being NULL so two concurrent discuss clicks can't both
        claim it; the loser sees rowcount 0 and cleans up its thread."""
        cur = self._conn.execute(
            "UPDATE interaction_feedback SET meta_thread_id = ? "
            "WHERE id = ? AND meta_thread_id IS NULL",
            (int(thread_id), int(fid)))
        self._safe_commit()
        return cur.rowcount > 0

    # ── Agent harness (2026-07-11): MCP connectors ──────────────

    def list_mcp_connectors(self, enabled_only=False):
        q = "SELECT * FROM mcp_connectors"
        if enabled_only:
            q += " WHERE enabled = 1"
        rows = self._conn.execute(q + " ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def upsert_mcp_connector(self, *, name, base_url, auth_header=None,
                             enabled=True):
        """auth_header semantics: None = PRESERVE the stored secret
        (the list route masks it, so clients cannot round-trip it);
        '' = explicitly clear; any other string = replace."""
        name = (name or "").strip().lower()
        if auth_header is None:
            self._conn.execute(
                "INSERT INTO mcp_connectors (name, base_url, "
                "auth_header, enabled) VALUES (?,?,'',?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "base_url=excluded.base_url, "
                "enabled=excluded.enabled",
                (name, (base_url or "").strip(), 1 if enabled else 0))
        else:
            self._conn.execute(
                "INSERT INTO mcp_connectors (name, base_url, "
                "auth_header, enabled) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "base_url=excluded.base_url, "
                "auth_header=excluded.auth_header, "
                "enabled=excluded.enabled",
                (name, (base_url or "").strip(), auth_header,
                 1 if enabled else 0))
        self._safe_commit()
        row = self._conn.execute(
            "SELECT id FROM mcp_connectors WHERE name = ?",
            (name,)).fetchone()
        return row["id"] if row else 0

    def delete_mcp_connector(self, name):
        cur = self._conn.execute(
            "DELETE FROM mcp_connectors WHERE name = ?",
            ((name or "").strip().lower(),))
        self._safe_commit()
        return cur.rowcount > 0

    # ── Slice 8: chat file attachments ──────────────────────────

    def append_chat_file(self, *, chat_message_id, filename, mime_type,
                         size_bytes, kind, pi_path, file_id=0, sha256=""):
        cur = self._conn.execute(
            "INSERT INTO chat_message_files (chat_message_id, filename, "
            "mime_type, size_bytes, kind, pi_path, file_id, sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(chat_message_id), filename, mime_type or "",
             int(size_bytes or 0), kind or "other", pi_path,
             int(file_id or 0), sha256 or ""),
        )
        self._safe_commit()
        return cur.lastrowid

    def chat_files_for_message_ids(self, message_ids):
        """Return {chat_message_id: [file_dict, ...]} for the given ids.
        Empty/missing ids return {}. Files are ordered by id ascending
        (insertion order) so the frontend can render them in the order
        the user attached them."""
        ids = [int(i) for i in (message_ids or []) if i is not None]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            "SELECT * FROM chat_message_files "
            "WHERE chat_message_id IN ({}) "
            "ORDER BY chat_message_id, id".format(placeholders),
            ids,
        ).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            out.setdefault(d["chat_message_id"], []).append(d)
        return out

    # ── Slice 3e: notifications ─────────────────────────────────

    def emit_notification(self, *, severity, title, body="",
                          rule_name, rule_key, related_table="",
                          related_id="", action_url=""):
        """Insert a notification idempotently. UNIQUE(rule_name, rule_key)
        means the same rule firing on the same key is a no-op (not a
        duplicate). Updates title/body if those changed (e.g., the count
        of open imports for a rollup notification grew)."""
        valid = ("info", "warn", "important")
        if severity not in valid:
            raise ValueError("severity must be one of {}".format(valid))
        self._conn.execute(
            "INSERT INTO notifications (severity, title, body, "
            "related_table, related_id, action_url, rule_name, rule_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(rule_name, rule_key) DO UPDATE SET "
            "severity=excluded.severity, title=excluded.title, "
            "body=excluded.body, related_table=excluded.related_table, "
            "related_id=excluded.related_id, action_url=excluded.action_url, "
            # A re-firing rule un-resolves its own notification: clear the
            # auto-resolver's archived_at so a recurring alert (LLM still
            # down, weather alert re-detected) reappears without a manual
            # un-archive. dismissed_at is left alone — that's a user action.
            "archived_at=NULL",
            (severity, title, body, related_table, related_id, action_url,
             rule_name, rule_key),
        )
        self._safe_commit()

    def list_notifications(self, *, include_dismissed=False, limit=100,
                            include_archived=False, include_snoozed=False):
        """Default: only currently-actionable notifications. A
        notification is hidden if it's dismissed, archived, OR snoozed
        with snoozed_until in the future."""
        sql = "SELECT * FROM notifications WHERE 1=1"
        if not include_dismissed:
            sql += " AND dismissed_at IS NULL"
        if not include_archived:
            sql += " AND archived_at IS NULL"
        if not include_snoozed:
            sql += (" AND (snoozed_until IS NULL OR "
                    "snoozed_until <= datetime('now'))")
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._conn.execute(sql, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def unread_notification_count(self):
        return self._conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE "
            "dismissed_at IS NULL AND archived_at IS NULL AND "
            "(snoozed_until IS NULL OR snoozed_until <= datetime('now'))"
        ).fetchone()[0]

    def dismiss_notification(self, notification_id):
        cur = self._conn.execute(
            "UPDATE notifications SET dismissed_at = datetime('now') "
            "WHERE id = ? AND dismissed_at IS NULL",
            (int(notification_id),),
        )
        self._safe_commit()
        return cur.rowcount > 0

    def dismiss_all_notifications(self):
        cur = self._conn.execute(
            "UPDATE notifications SET dismissed_at = datetime('now') "
            "WHERE dismissed_at IS NULL"
        )
        self._safe_commit()
        return cur.rowcount

    def archive_notification(self, notification_id):
        """Archive — different intent than dismiss. The notification is
        acknowledged AND kept out of the actionable queue. Survives the
        rule re-firing, since `archived_at` is preserved across
        UNIQUE(rule, key) upserts (we never UPDATE archived_at to NULL
        in the upsert path)."""
        cur = self._conn.execute(
            "UPDATE notifications SET archived_at = datetime('now') "
            "WHERE id = ? AND archived_at IS NULL",
            (int(notification_id),),
        )
        self._safe_commit()
        return cur.rowcount > 0

    def snooze_notification(self, notification_id, until_iso):
        """Hide until a future timestamp. Once snoozed_until passes,
        the notification reappears in list_notifications (the WHERE
        clause re-includes it). Caller passes the ISO timestamp."""
        cur = self._conn.execute(
            "UPDATE notifications SET snoozed_until = ? WHERE id = ?",
            (until_iso, int(notification_id)),
        )
        self._safe_commit()
        return cur.rowcount > 0

    def touch_notification(self, notification_id):
        """Pull a notification back to the actionable queue: clear
        dismissed_at, snoozed_until, archived_at all at once."""
        cur = self._conn.execute(
            "UPDATE notifications SET dismissed_at = NULL, "
            "snoozed_until = NULL, archived_at = NULL WHERE id = ?",
            (int(notification_id),),
        )
        self._safe_commit()
        return cur.rowcount > 0

    # ── Slice 9.6 CP2 (2026-05-19): chat message redaction ─────

    def delete_chat_message(self, message_id):
        """Delete a single chat_messages row + its attachments. Used
        by overseer's redact_chat_message tool when Tory has asked
        for a message to be scrubbed. FK CASCADE is off on this DB,
        so attachments must be deleted explicitly first."""
        message_id = int(message_id)
        with self._write_lock:
            self._conn.execute(
                "DELETE FROM chat_message_files WHERE chat_message_id = ?",
                (message_id,),
            )
            cur = self._conn.execute(
                "DELETE FROM chat_messages WHERE id = ?",
                (message_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def redact_chat_attachment(self, *, file_id=None, message_id=None):
        """Delete a chat attachment (file) without deleting the message.
        Pass either file_id (single file) OR message_id (all files on
        that message). Returns the count deleted.

        The underlying file on disk under /uploads is NOT touched here —
        that's the responsibility of a separate file-cleanup pass. We
        only remove the DB linkage so the file no longer appears in
        chat history or LLM prompt construction.
        """
        with self._write_lock:
            if file_id is not None:
                cur = self._conn.execute(
                    "DELETE FROM chat_message_files WHERE id = ?",
                    (int(file_id),),
                )
            elif message_id is not None:
                cur = self._conn.execute(
                    "DELETE FROM chat_message_files "
                    "WHERE chat_message_id = ?",
                    (int(message_id),),
                )
            else:
                return 0
            self._conn.commit()
            return cur.rowcount

    # ── Slice 9.6 CP1 (2026-05-19): notification responses ─────

    def add_notification_response(self, *, notification_id, action_kind,
                                   action_label="", response_payload=None):
        """Log Tory's click/response to a custom action button on a
        notification. response_payload is dict (auto-JSON-encoded).
        Returns the new notification_responses.id."""
        payload_json = json.dumps(response_payload or {},
                                   ensure_ascii=False, sort_keys=True)
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT INTO notification_responses "
                "(notification_id, action_kind, action_label, "
                "response_payload_json) VALUES (?, ?, ?, ?)",
                (int(notification_id), action_kind,
                 action_label or "", payload_json),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_pending_notification_responses(self, *, limit=50):
        """Return responses that overseer hasn't marked processed yet.
        Joined to the notification's title + rule + body so overseer
        sees the full context in one call.

        Slice 9.6 CP3: read tool for overseer. After reading, overseer
        should call mark_notification_responses_processed with the
        returned ids to dequeue.
        """
        rows = self._conn.execute(
            "SELECT nr.id, nr.notification_id, nr.action_kind, "
            "  nr.action_label, nr.response_payload_json, nr.taken_at, "
            "  nr.local_taken_at, "
            "  n.rule_name, n.rule_key, n.title as notif_title, "
            "  n.body as notif_body, n.related_table, n.related_id, "
            "  n.actions_json "
            "FROM notification_responses nr "
            "LEFT JOIN notifications n ON n.id = nr.notification_id "
            "WHERE nr.processed_by_overseer_at IS NULL "
            "ORDER BY nr.id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["response_payload"] = json.loads(
                    d.pop("response_payload_json", None) or "{}")
            except Exception:
                d["response_payload"] = {}
            try:
                d["actions"] = json.loads(d.pop("actions_json", None) or "[]")
            except Exception:
                d["actions"] = []
            out.append(d)
        return out

    def pending_notification_responses_count(self):
        """Count of unprocessed responses — surfaced in working memory
        freshness so overseer notices new responses without polling."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM notification_responses "
            "WHERE processed_by_overseer_at IS NULL"
        ).fetchone()[0]

    def mark_notification_responses_processed(self, *, response_ids):
        """Mark a list of response ids as read by overseer. Idempotent."""
        ids = [int(i) for i in (response_ids or []) if i is not None]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self._write_lock:
            cur = self._conn.execute(
                f"UPDATE notification_responses "
                f"SET processed_by_overseer_at = datetime('now') "
                f"WHERE id IN ({placeholders}) "
                f"AND processed_by_overseer_at IS NULL",
                ids,
            )
            self._conn.commit()
            return cur.rowcount

    def emit_notification(self, *, severity, title, body="",
                           rule_name="overseer-emit", rule_key=None,
                           related_table="", related_id="",
                           action_url="", actions=None):
        """Slice 9.6 CP3: insert a notification with custom action
        buttons. If rule_key is None we auto-generate one so each
        emit creates a distinct row (otherwise UNIQUE(rule_name,
        rule_key) coalesces multiple emits into one).

        actions: list of dicts {label, kind, payload?}. JSON-encoded
        into actions_json. The frontend renders them as buttons.

        Returns the new notification.id."""
        actions = actions or []
        actions_json = json.dumps(actions, ensure_ascii=False, sort_keys=True)
        if not rule_key:
            # Auto-key per insert so emits don't coalesce
            import uuid
            rule_key = f"emit-{uuid.uuid4().hex[:12]}"
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT INTO notifications "
                "(severity, title, body, related_table, related_id, "
                "action_url, rule_name, rule_key, actions_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (severity, title, body, related_table, related_id,
                 action_url, rule_name, rule_key, actions_json),
            )
            self._conn.commit()
            return cur.lastrowid

    def auto_archive_stale_notifications(self, *, rule_name,
                                          older_than_days):
        """Polish CP2: archive notifications of a given rule that are
        older than N days AND haven't been touched (dismissed/archived/
        snoozed all NULL). Returns the number archived. Idempotent —
        already-archived rows aren't re-touched."""
        cur = self._conn.execute(
            "UPDATE notifications SET archived_at = datetime('now') "
            "WHERE rule_name = ? "
            "  AND created_at < datetime('now', ?) "
            "  AND archived_at IS NULL "
            "  AND dismissed_at IS NULL "
            "  AND (snoozed_until IS NULL OR "
            "       snoozed_until <= datetime('now'))",
            (rule_name, "-{} days".format(int(older_than_days))),
        )
        self._safe_commit()
        return cur.rowcount

    def auto_resolve_stale_rules(self, *, current_rule_keys):
        """Polish CP2: when an evaluation cycle produces a NEW set of
        active rule_keys for a given rule_name, any prior actionable
        notifications for that rule whose key is no longer in the
        active set get auto-archived. Resolves "I fixed the project
        but the notification is still glaring at me" silently.

        Args:
            current_rule_keys: dict[rule_name -> set(keys-now-firing)]

        Returns the total number of notifications auto-resolved.
        """
        if not current_rule_keys:
            return 0
        total = 0
        for rule_name, active_keys in current_rule_keys.items():
            if not active_keys:
                # Rule didn't fire at all this cycle — auto-resolve
                # ALL still-actionable notifications for it.
                cur = self._conn.execute(
                    "UPDATE notifications SET archived_at = datetime('now') "
                    "WHERE rule_name = ? "
                    "  AND archived_at IS NULL "
                    "  AND dismissed_at IS NULL "
                    "  AND (snoozed_until IS NULL OR "
                    "       snoozed_until <= datetime('now'))",
                    (rule_name,),
                )
                total += cur.rowcount
                continue
            # Rule fired for SOME keys — auto-resolve actionable rows
            # for keys NOT in the current set.
            placeholders = ",".join("?" for _ in active_keys)
            params = [rule_name] + list(active_keys)
            cur = self._conn.execute(
                "UPDATE notifications SET archived_at = datetime('now') "
                "WHERE rule_name = ? "
                "  AND rule_key NOT IN ({}) "
                "  AND archived_at IS NULL "
                "  AND dismissed_at IS NULL "
                "  AND (snoozed_until IS NULL OR "
                "       snoozed_until <= datetime('now'))".format(placeholders),
                params,
            )
            total += cur.rowcount
        self._safe_commit()
        return total

    # ── Slice 3f: dialectic ────────────────────────────────────

    def add_dialectic(self, *, artifact_type, artifact_id=None,
                      purpose="", opus_model="", gemma_model="",
                      opus_text="", gemma_text="",
                      opus_confidence="med", gemma_confidence="med",
                      severity="none", similarity=1.0,
                      diff_summary="", source_context="",
                      opus_cost_usd=0.0, gemma_cost_usd=0.0):
        cur = self._conn.execute(
            "INSERT INTO dialectic_open (artifact_type, artifact_id, "
            "purpose, opus_model, gemma_model, opus_text, gemma_text, "
            "opus_confidence, gemma_confidence, severity, similarity, "
            "diff_summary, source_context, opus_cost_usd, gemma_cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_type, artifact_id, purpose, opus_model, gemma_model,
             opus_text, gemma_text, opus_confidence, gemma_confidence,
             severity, float(similarity), diff_summary, source_context,
             float(opus_cost_usd), float(gemma_cost_usd)),
        )
        self._safe_commit()
        return cur.lastrowid

    def list_dialectics(self, *, status=None, severity=None,
                        artifact_type=None, limit=100, offset=0):
        sql = "SELECT * FROM dialectic_open"
        params: list = []
        wheres: list[str] = []
        if status:
            wheres.append("status = ?")
            params.append(status)
        if severity:
            wheres.append("severity = ?")
            params.append(severity)
        if artifact_type:
            wheres.append("artifact_type = ?")
            params.append(artifact_type)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_dialectic(self, dialectic_id):
        row = self._conn.execute(
            "SELECT * FROM dialectic_open WHERE id = ?", (int(dialectic_id),)
        ).fetchone()
        return dict(row) if row else None

    def resolve_dialectic(self, dialectic_id, *, resolution,
                          resolution_text="", resolved_by="user"):
        """resolution: opus | gemma | third | productive

        'productive' = user marks the disagreement as productive (don't
        resolve). Status moves to 'productive', the dialectic stays
        visible as a live caveat in working memory.
        """
        valid = ("opus", "gemma", "third", "productive")
        if resolution not in valid:
            raise ValueError("resolution must be one of {}".format(valid))
        new_status = "productive" if resolution == "productive" else "resolved"
        cur = self._conn.execute(
            "UPDATE dialectic_open SET status=?, resolution=?, "
            "resolution_text=?, resolved_at=datetime('now'), "
            "resolved_by=? WHERE id=? AND status='open'",
            (new_status, resolution, resolution_text, resolved_by,
             int(dialectic_id)),
        )
        self._safe_commit()
        return cur.rowcount > 0

    def dialectic_counts(self):
        rows = self._conn.execute(
            "SELECT status, severity, COUNT(*) AS n FROM dialectic_open "
            "GROUP BY status, severity"
        ).fetchall()
        out = {"open": 0, "open_significant": 0, "open_minor": 0,
               "resolved": 0, "productive": 0, "total": 0}
        for r in rows:
            n = r["n"]
            out["total"] += n
            if r["status"] == "open":
                out["open"] += n
                if r["severity"] == "significant":
                    out["open_significant"] += n
                elif r["severity"] == "minor":
                    out["open_minor"] += n
            elif r["status"] == "resolved":
                out["resolved"] += n
            elif r["status"] == "productive":
                out["productive"] += n
        return out

    # ── Slice 3f.5: overseer journal ────────────────────────────

    def add_journal_entry(self, *, body, instance_id="",
                          triggered_by="tick", provisionality="med",
                          referenced_artifacts=None, tick_summary=None,
                          backend="", model="", cost_usd=0.0,
                          latency_ms=0):
        """Append-only. NEVER UPDATE OR DELETE these rows."""
        cur = self._conn.execute(
            "INSERT INTO overseer_journal (body, instance_id, "
            "triggered_by, provisionality, referenced_artifacts, "
            "tick_summary_json, backend, model, cost_usd, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body, instance_id, triggered_by, provisionality,
             json.dumps(referenced_artifacts or []),
             json.dumps(tick_summary or {}),
             backend, model, float(cost_usd), int(latency_ms)),
        )
        self._safe_commit()
        return cur.lastrowid

    def recent_journal_entries(self, limit=10):
        rows = self._conn.execute(
            "SELECT * FROM overseer_journal ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def all_journal_entries(self, limit=500):
        rows = self._conn.execute(
            "SELECT * FROM overseer_journal ORDER BY id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def journal_count(self):
        return self._conn.execute(
            "SELECT COUNT(*) FROM overseer_journal"
        ).fetchone()[0]

    def get_journal_entry(self, entry_id):
        row = self._conn.execute(
            "SELECT * FROM overseer_journal WHERE id = ?", (int(entry_id),),
        ).fetchone()
        return dict(row) if row else None

    # ── Slice 3f.5 #4: known blindspots ─────────────────────────

    def list_blindspots(self, *, active_only=True, limit=200):
        sql = "SELECT * FROM known_blindspots"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY confidence DESC, id ASC LIMIT ?"
        rows = self._conn.execute(sql, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def get_blindspot(self, blindspot_id):
        row = self._conn.execute(
            "SELECT * FROM known_blindspots WHERE id = ?",
            (int(blindspot_id),)
        ).fetchone()
        return dict(row) if row else None

    def upsert_blindspot(self, *, id=None, model_pattern, body,
                         topic_pattern="", direction="general",
                         confidence_adjustment=0, rationale="",
                         confidence="med", source="user",
                         is_active=True):
        if id is not None:
            self._conn.execute(
                "UPDATE known_blindspots SET model_pattern=?, "
                "topic_pattern=?, direction=?, confidence_adjustment=?, "
                "body=?, rationale=?, confidence=?, source=?, is_active=? "
                "WHERE id=?",
                (model_pattern, topic_pattern, direction,
                 int(confidence_adjustment), body, rationale,
                 confidence, source, 1 if is_active else 0, int(id)),
            )
            self._safe_commit()
            return int(id)
        cur = self._conn.execute(
            "INSERT INTO known_blindspots (model_pattern, topic_pattern, "
            "direction, confidence_adjustment, body, rationale, "
            "confidence, source, is_active) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model_pattern, topic_pattern, direction,
             int(confidence_adjustment), body, rationale,
             confidence, source, 1 if is_active else 0),
        )
        self._safe_commit()
        return cur.lastrowid

    def set_blindspot_active(self, blindspot_id, is_active):
        cur = self._conn.execute(
            "UPDATE known_blindspots SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, int(blindspot_id)),
        )
        self._safe_commit()
        return cur.rowcount > 0

    def record_blindspot_application(self, blindspot_id):
        """Bump apply_count + last_applied_at when a blindspot is
        actually surfaced as a caveat. Used for prioritization later
        (frequently-applied blindspots bubble up)."""
        self._conn.execute(
            "UPDATE known_blindspots SET apply_count = apply_count + 1, "
            "last_applied_at = datetime('now') WHERE id = ?",
            (int(blindspot_id),),
        )
        self._safe_commit()

    def log_correction(self, *, model="", artifact_table="",
                       artifact_id=None, topic="", what_was_wrong,
                       user_correction="", severity="med",
                       source="manual"):
        cur = self._conn.execute(
            "INSERT INTO interpretation_corrections (model, "
            "artifact_table, artifact_id, topic, what_was_wrong, "
            "user_correction, severity, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (model, artifact_table, artifact_id, topic, what_was_wrong,
             user_correction, severity, source),
        )
        self._safe_commit()
        return cur.lastrowid

    def list_corrections(self, *, limit=100, undistilled_only=False):
        sql = "SELECT * FROM interpretation_corrections"
        if undistilled_only:
            sql += " WHERE used_in_blindspot_id IS NULL"
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._conn.execute(sql, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def correction_count(self, *, undistilled_only=False):
        sql = "SELECT COUNT(*) FROM interpretation_corrections"
        if undistilled_only:
            sql += " WHERE used_in_blindspot_id IS NULL"
        return self._conn.execute(sql).fetchone()[0]

    def mark_corrections_distilled(self, *, correction_ids, blindspot_id):
        """3i CP2: link corrections to the blindspot they generated.
        Caller passes the new blindspots.id after a confirm. Idempotent
        (only updates rows still NULL)."""
        if not correction_ids:
            return 0
        placeholders = ",".join("?" for _ in correction_ids)
        params = [int(blindspot_id), *[int(i) for i in correction_ids]]
        cur = self._conn.execute(
            "UPDATE interpretation_corrections SET used_in_blindspot_id = ? "
            f"WHERE id IN ({placeholders}) AND used_in_blindspot_id IS NULL",
            params,
        )
        self._safe_commit()
        return cur.rowcount

    def imported_session_count(self, source=None):
        if source:
            return self._conn.execute(
                "SELECT COUNT(*) FROM imported_sessions WHERE source = ?",
                (source,),
            ).fetchone()[0]
        return self._conn.execute(
            "SELECT COUNT(*) FROM imported_sessions"
        ).fetchone()[0]

    # ── overall snapshot for /status ────────────────────────────

    def overseer_snapshot(self):
        def _count(table):
            try:
                return self._conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                return 0
        return {
            "summaries_gist": _count("summaries_gist"),
            "summaries_theme": _count("summaries_theme"),
            "summaries_episode": _count("summaries_episode"),
            "open_questions": _count("open_questions"),
            "patterns": _count("patterns"),
            "drift_observations": _count("drift_observations"),
            "future_overseer_notes": _count("future_overseer_notes"),
            "llm_calls": _count("llm_calls"),
            "tags": _count("tags"),
            "raw_pointers": _count("raw_pointers"),
            "processed_sessions": _count("processed_sessions"),
            "processed_notes": _count("processed_notes"),
            "imported_sessions": _count("imported_sessions"),
            "processed_imported_sessions": _count(
                "processed_imported_sessions"),
            "imported_project_settings": _count("imported_project_settings"),
            "automation_rollups": _count("automation_rollups"),
            "chat_messages": _count("chat_messages"),
            "notifications": _count("notifications"),
            "notifications_unread": self.unread_notification_count(),
            "dialectic_open": _count("dialectic_open"),
            "dialectic_open_significant": self._conn.execute(
                "SELECT COUNT(*) FROM dialectic_open WHERE status='open' "
                "AND severity='significant'"
            ).fetchone()[0],
            "overseer_journal": _count("overseer_journal"),
            "evidence_for_question": _count("evidence_for_question"),
            "known_blindspots": _count("known_blindspots"),
            "interpretation_corrections": _count(
                "interpretation_corrections"),
            "pending_interpretations": _count("pending_interpretations"),
            "pending_interpretations_pending": self._conn.execute(
                "SELECT COUNT(*) FROM pending_interpretations "
                "WHERE status='pending'"
            ).fetchone()[0],
            "insight_scans": _count("insight_scans"),
        }

    # ── Slice 3h: pending interpretations + scan log ────────────

    # Slice 9.7 (2026-05-19/20): 'merge_proposal' added so overseer
    # can route proposed project merges through the standard
    # pending_interpretations review flow (accept/reject in Hub
    # Insights). Per overseer's spec: "DOES NOT execute the merge —
    # writes a row for Tory to accept/reject."
    VALID_INSIGHT_KINDS = ("theme", "pattern", "drift", "blindspot",
                            "merge_proposal")
    VALID_INTERP_STATUSES = (
        "pending", "confirmed", "rejected", "edited", "superseded",
    )

    def insert_pending_interpretation(
        self, *,
        kind, title, body, confidence="med", direction="",
        rationale="", proposed_by, source_kind="gist-arc",
        source_project="", source_window_start=None,
        source_window_end=None, source_pointer_ids=None,
        source_chat_message_id=None,
        # 3i CP2: blindspot-kind specific fields
        bs_model_pattern="", bs_topic_pattern="",
        bs_confidence_adjustment=0,
    ):
        """Add a candidate to the review queue. Returns the new id, OR
        None if a duplicate (same kind+normalized-title) is already
        pending."""
        if kind not in self.VALID_INSIGHT_KINDS:
            raise ValueError("kind must be one of {}".format(
                self.VALID_INSIGHT_KINDS))
        # Dedup: don't re-propose what's already pending.
        norm = (title or "").strip().lower()
        existing = self._conn.execute(
            "SELECT id FROM pending_interpretations "
            "WHERE kind = ? AND lower(trim(title)) = ? "
            "AND status = 'pending'",
            (kind, norm),
        ).fetchone()
        if existing:
            return None
        import json as _json
        cur = self._conn.execute(
            "INSERT INTO pending_interpretations ("
            "  kind, title, body, confidence, direction, rationale,"
            "  proposed_by, source_kind, source_project,"
            "  source_window_start, source_window_end, source_pointer_ids,"
            "  source_chat_message_id,"
            "  bs_model_pattern, bs_topic_pattern, bs_confidence_adjustment"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, title, body, _norm_confidence(confidence),
             direction or "", rationale or "", proposed_by,
             source_kind, source_project or "",
             source_window_start, source_window_end,
             _json.dumps(source_pointer_ids or []),
             source_chat_message_id,
             bs_model_pattern or "", bs_topic_pattern or "",
             int(bs_confidence_adjustment or 0)),
        )
        self._safe_commit()
        return cur.lastrowid

    def list_pending_interpretations(
        self, *, status=None, kind=None, project=None, limit=200,
    ):
        sql = "SELECT * FROM pending_interpretations WHERE 1=1"
        params = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if project:
            sql += " AND source_project = ?"
            params.append(project)
        sql += " ORDER BY proposed_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_pending_interpretation(self, interp_id):
        row = self._conn.execute(
            "SELECT * FROM pending_interpretations WHERE id = ?",
            (int(interp_id),),
        ).fetchone()
        return dict(row) if row else None

    def update_pending_interpretation_status(
        self, *, interp_id, status, reviewed_by="user",
        review_note="", edit_title="", edit_body="",
        applied_table="", applied_id=None,
    ):
        if status not in self.VALID_INTERP_STATUSES:
            raise ValueError("status must be one of {}".format(
                self.VALID_INTERP_STATUSES))
        self._conn.execute(
            "UPDATE pending_interpretations SET "
            "  status = ?, reviewed_at = datetime('now'), "
            "  reviewed_by = ?, review_note = ?, "
            "  edit_title = ?, edit_body = ?, "
            "  applied_table = ?, applied_id = ? "
            "WHERE id = ?",
            (status, reviewed_by, review_note or "",
             edit_title or "", edit_body or "",
             applied_table or "", applied_id, int(interp_id)),
        )
        self._safe_commit()

    def log_insight_scan(
        self, *, scan_kind, project="", window_start=None,
        window_end=None, gists_seen=0, candidates_proposed=0,
        candidates_deduped=0, cost_usd=0.0,
        triggered_by="manual", ok=True, error="",
    ):
        cur = self._conn.execute(
            "INSERT INTO insight_scans ("
            "  scan_kind, project, window_start, window_end,"
            "  gists_seen, candidates_proposed, candidates_deduped,"
            "  cost_usd, triggered_by, ok, error"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_kind, project or "", window_start, window_end,
             int(gists_seen), int(candidates_proposed),
             int(candidates_deduped), float(cost_usd),
             triggered_by, 1 if ok else 0, error or ""),
        )
        self._safe_commit()
        return cur.lastrowid

    def recent_insight_scans(self, *, project=None, limit=20):
        sql = "SELECT * FROM insight_scans WHERE 1=1"
        params = []
        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY scanned_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def gists_for_project(self, *, project, since_iso=None, limit=200):
        """Return gists tagged with project:<project>, optionally since
        a UTC timestamp. Newest first."""
        sql = (
            "SELECT g.* FROM summaries_gist g "
            "JOIN tags t ON t.table_name = 'summaries_gist' "
            "  AND t.row_id = g.id "
            "WHERE t.tag = ? "
        )
        params = ["project:" + project]
        if since_iso:
            sql += " AND g.created_at >= ? "
            params.append(since_iso)
        sql += " ORDER BY g.created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        # De-dup just in case multiple project tags collide.
        seen = set()
        out = []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(dict(r))
        return out

    # ── Slice 4 CP1a: project_summaries ─────────────────────────

    def list_distinct_imported_projects(self):
        """Distinct project tags across imported_sessions. Used by
        project_summary.refresh_all to know which projects to roll up."""
        rows = self._conn.execute(
            "SELECT DISTINCT project FROM imported_sessions "
            "WHERE project != '' ORDER BY project"
        ).fetchall()
        return [r["project"] for r in rows]

    def imported_sessions_for_project(self, project):
        """Return ALL imported_sessions rows for a project, oldest first.
        Used by project_summary aggregation; project rollups read each
        row's metadata_json for the extended (token / file) stats."""
        rows = self._conn.execute(
            "SELECT * FROM imported_sessions WHERE project = ? "
            "ORDER BY started_at ASC NULLS LAST, imported_at ASC",
            (project,),
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_project_summary(self, *, project, **fields):
        """Insert or update a project_summaries row. `fields` keys must
        match column names; stats_updated_at is set automatically.
        Caller is responsible for serializing JSON columns to strings."""
        if not project:
            raise ValueError("project required")
        # Always bump the timestamp.
        fields["stats_updated_at"] = "datetime('now')"
        # Build SQL. stats_updated_at uses an SQL expression so we splice
        # it into the column list separately from bind params.
        cols = ["project"]
        placeholders = ["?"]
        params: list = [project]
        update_pairs = []
        for k, v in fields.items():
            cols.append(k)
            if k == "stats_updated_at":
                placeholders.append("datetime('now')")
                update_pairs.append("{}=datetime('now')".format(k))
            else:
                placeholders.append("?")
                params.append(v)
                update_pairs.append("{}=excluded.{}".format(k, k))
        sql = (
            "INSERT INTO project_summaries ({cols}) VALUES ({ph}) "
            "ON CONFLICT(project) DO UPDATE SET {up}"
        ).format(
            cols=", ".join(cols),
            ph=", ".join(placeholders),
            up=", ".join(update_pairs),
        )
        self._conn.execute(sql, params)
        self._safe_commit()
        return project

    def get_project_summary(self, project):
        """Look up a project_summaries row by name.

        Bug fix 2026-05-16 (task 4 diagnostic): callers from the chat
        tool surface pass the *tag* form (`openmuscle-flexgrid`) read
        from working_memory.top_projects, while the table's PK is the
        *display* form (`OpenMuscle-FlexGrid`). SQLite's default
        BINARY collation made exact lookups fail. We now try, in
        order:
          1. exact match
          2. case-insensitive match (COLLATE NOCASE)
          3. slug-normalized match (lowercase, spaces↔hyphens)
        First hit wins. Returns None if all three miss.
        """
        if not project:
            return None
        cur = self._conn
        # 1. exact
        row = cur.execute(
            "SELECT * FROM project_summaries WHERE project = ?",
            (project,),
        ).fetchone()
        if row:
            return dict(row)
        # 2. case-insensitive
        row = cur.execute(
            "SELECT * FROM project_summaries "
            "WHERE project = ? COLLATE NOCASE",
            (project,),
        ).fetchone()
        if row:
            return dict(row)
        # 3. slug-normalized: lowercase + treat hyphens and spaces
        #    as interchangeable. Compare normalized PK against
        #    normalized input.
        wanted = project.lower().replace(" ", "-")
        row = cur.execute(
            "SELECT * FROM project_summaries "
            "WHERE REPLACE(LOWER(project), ' ', '-') = ?",
            (wanted,),
        ).fetchone()
        return dict(row) if row else None

    def list_project_summaries(self, *, order_by="last_active_at",
                               descending=True, limit=None):
        """List project summaries. ``order_by`` is one of a whitelisted
        column set. ``limit`` is an optional cap on rows returned.

        Bug fix 2026-05-16 (task 4 diagnostic): the chat tool surface
        ``list_active_projects`` passes ``limit=`` per its declared
        schema but the original signature didn't accept it, throwing
        TypeError on every call. The limit is applied SQL-side for
        efficiency.
        """
        # Whitelist to prevent SQL injection via order_by.
        allowed = {
            "last_active_at", "session_count", "cost_usd_estimate",
            "total_minutes", "total_messages", "first_active_at",
            "stats_updated_at", "project",
        }
        if order_by not in allowed:
            order_by = "last_active_at"
        direction = "DESC" if descending else "ASC"
        # NULLS LAST keeps freshly-rolled-up projects at the top when
        # ordering by *_active_at columns (those with NULL haven't run yet).
        sql = "SELECT * FROM project_summaries ORDER BY {} {} NULLS LAST".format(
            order_by, direction,
        )
        params = ()
        if limit is not None:
            try:
                lim = max(1, int(limit))
            except (TypeError, ValueError):
                lim = 50
            sql = sql + " LIMIT ?"
            params = (lim,)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Older SQLite without NULLS LAST support: retry without it.
            sql = sql.replace(" NULLS LAST", "")
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def delete_project_summary(self, project):
        cur = self._conn.execute(
            "DELETE FROM project_summaries WHERE project = ?", (project,)
        )
        self._safe_commit()
        return cur.rowcount

    # ── Slice 5: temporal_narratives ────────────────────────────

    def add_temporal_narrative(self, *, kind, period_start, period_end,
                                period_label, narrative, cost_usd=0.0,
                                model="", triggered_by="loop",
                                local_created_at=""):
        """Insert a temporal_narratives row. Returns the new id, or
        None if a row for (kind, period_label) already exists (the
        UNIQUE constraint protects against double-generation)."""
        if kind not in ("daily", "weekly", "monthly", "yearly"):
            raise ValueError("kind must be daily/weekly/monthly/yearly")
        try:
            cur = self._conn.execute(
                "INSERT INTO temporal_narratives "
                "(kind, period_start, period_end, period_label, "
                " narrative, cost_usd, model, triggered_by, "
                " local_created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, period_start, period_end, period_label,
                 narrative, float(cost_usd or 0), model, triggered_by,
                 local_created_at),
            )
            self._safe_commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # UNIQUE(kind, period_label) conflict

    def get_temporal_narrative(self, kind, period_label):
        row = self._conn.execute(
            "SELECT * FROM temporal_narratives "
            "WHERE kind = ? AND period_label = ?",
            (kind, period_label),
        ).fetchone()
        return dict(row) if row else None

    def list_temporal_narratives(self, *, kind=None, limit=50):
        sql = "SELECT * FROM temporal_narratives"
        params: list = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        # Slice 14.7.4 (2026-05-26): order by period_start DESC so
        # the list reads chronologically (newest period first). Prior
        # order by created_at DESC sorted by regenerate-time which
        # scrambled history every time we did a bulk re-run.
        sql += " ORDER BY period_start DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def latest_temporal_narrative(self, kind):
        """Used by the loop to short-circuit: if there's already a
        row whose period_label matches the period we'd generate, skip."""
        row = self._conn.execute(
            "SELECT * FROM temporal_narratives WHERE kind = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return dict(row) if row else None

    # ── Looper log (2026-06-05) ─────────────────────────────────
    #
    # Tory's separate Claude Code /loop session writes here so each
    # fresh iteration knows what its predecessor did + what to pick
    # up. Cheap journaling — no indexes that would slow it down.

    def next_looper_iteration_number(self):
        """Read max(iteration_number) + 1. Returns 1 if table is empty."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(iteration_number), 0) + 1 "
            "FROM looper_log"
        ).fetchone()
        return int(row[0]) if row else 1

    def start_looper_iteration(self, *, mode="general", session_id="",
                                  model="", local_started_at=""):
        """Insert a new looper_log row at the start of an iteration.
        Returns (id, iteration_number)."""
        iter_n = self.next_looper_iteration_number()
        cur = self._conn.execute(
            "INSERT INTO looper_log "
            "(iteration_number, mode, session_id, model, "
            " local_started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (iter_n, str(mode), str(session_id), str(model),
             str(local_started_at)),
        )
        self._safe_commit()
        return {"id": cur.lastrowid, "iteration_number": iter_n}

    def finish_looper_iteration(self, *, id, summary="",
                                   work_done=None, followups=None,
                                   files_changed=None,
                                   llm_calls_estimate=0,
                                   cost_usd_estimate=0.0,
                                   escalations=None,
                                   local_ended_at=""):
        """Mark the iteration done + write its outputs."""
        self._conn.execute(
            "UPDATE looper_log SET "
            "  ended_at = datetime('now'), "
            "  local_ended_at = ?, "
            "  summary = ?, "
            "  work_done_json = ?, "
            "  followups_json = ?, "
            "  files_changed_json = ?, "
            "  llm_calls_estimate = ?, "
            "  cost_usd_estimate = ?, "
            "  escalations_json = ? "
            "WHERE id = ?",
            (
                str(local_ended_at),
                str(summary)[:5000],
                json.dumps(work_done or []),
                json.dumps(followups or []),
                json.dumps(files_changed or []),
                int(llm_calls_estimate or 0),
                float(cost_usd_estimate or 0.0),
                json.dumps(escalations or []),
                int(id),
            ),
        )
        self._safe_commit()
        return {"ok": True, "id": int(id)}

    def recent_looper_entries(self, *, limit=10):
        rows = self._conn.execute(
            "SELECT * FROM looper_log "
            "ORDER BY iteration_number DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Parse the JSON cols for the caller's convenience.
            for k in ("work_done_json", "followups_json",
                       "files_changed_json", "escalations_json"):
                try:
                    d[k.replace("_json", "")] = json.loads(
                        d.get(k) or "[]")
                except Exception:
                    d[k.replace("_json", "")] = []
            out.append(d)
        return out

    # ── Sub-agent tier registry (2026-05-27) ────────────────────
    #
    # Tory's directive: run B/C agents as cheap as possible, with a
    # human-pull upgrade trigger when output is poor. Tier choices
    # persist across restarts so a system reboot doesn't reset a
    # manual upgrade.

    _VALID_TIERS = ("flash", "glm", "sonnet", "opus")

    def get_sub_agent_tier(self, agent_type, agent_name):
        """Return the row for one sub-agent. None if no row exists
        (which means the agent hasn't been seeded — caller should
        seed-then-fetch via ensure_sub_agent_tier instead)."""
        row = self._conn.execute(
            "SELECT * FROM sub_agent_tiers "
            "WHERE agent_type = ? AND agent_name = ?",
            (str(agent_type), str(agent_name)),
        ).fetchone()
        return dict(row) if row else None

    def ensure_sub_agent_tier(self, agent_type, agent_name,
                                default_tier="flash",
                                default_notes=""):
        """Get the tier row; insert with `default_tier` if missing.
        Returns the row. Idempotent."""
        existing = self.get_sub_agent_tier(agent_type, agent_name)
        if existing:
            return existing
        if default_tier not in self._VALID_TIERS:
            default_tier = "flash"
        self._conn.execute(
            "INSERT OR IGNORE INTO sub_agent_tiers "
            "(agent_type, agent_name, model_tier, tier_set_by, notes) "
            "VALUES (?, ?, ?, 'default', ?)",
            (str(agent_type), str(agent_name), default_tier,
             str(default_notes)),
        )
        self._safe_commit()
        return self.get_sub_agent_tier(agent_type, agent_name)

    def set_sub_agent_tier(self, agent_type, agent_name, tier, *,
                            set_by="human", notes=""):
        """Change a sub-agent's tier. Returns the updated row.
        Raises ValueError on bad tier."""
        if tier not in self._VALID_TIERS:
            raise ValueError(
                f"tier must be one of {self._VALID_TIERS}, got {tier!r}")
        # Make sure the row exists first.
        self.ensure_sub_agent_tier(agent_type, agent_name)
        self._conn.execute(
            "UPDATE sub_agent_tiers SET model_tier = ?, "
            "  tier_set_at = datetime('now'), tier_set_by = ?, "
            "  notes = ? "
            "WHERE agent_type = ? AND agent_name = ?",
            (tier, str(set_by), str(notes), str(agent_type),
             str(agent_name)),
        )
        self._safe_commit()
        return self.get_sub_agent_tier(agent_type, agent_name)

    def record_sub_agent_invocation(self, agent_type, agent_name,
                                      model_used):
        """Update the tier row with the actual model used + last-run
        timestamp + bump invocation_count. Called by dispatch paths
        after every successful run. Fire-and-forget — never raises."""
        try:
            self.ensure_sub_agent_tier(agent_type, agent_name)
            self._conn.execute(
                "UPDATE sub_agent_tiers SET "
                "  last_model_used = ?, "
                "  last_invoked_at = datetime('now'), "
                "  invocation_count = invocation_count + 1 "
                "WHERE agent_type = ? AND agent_name = ?",
                (str(model_used or ""), str(agent_type),
                 str(agent_name)),
            )
            self._safe_commit()
        except Exception as e:
            log.warning("record_sub_agent_invocation %s/%s failed: %s",
                        agent_type, agent_name, e)

    def list_sub_agent_tiers(self):
        """Every row. Used by /sub-agents listing."""
        rows = self._conn.execute(
            "SELECT * FROM sub_agent_tiers "
            "ORDER BY agent_type ASC, agent_name ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def sub_agent_performance(self, agent_type, agent_name, *,
                                last_n=10):
        """Recent quality_rating signal for one sub-agent. Reads from
        sibling_tasks where the B/C agent dispatched the task.

        For B-agents: claimed_by = 'b-agent:<name>'.
        For C-agents: claimed_by = 'c-agent:<name>'.

        Returns TWO averages:
          - avg_rating         — over all `recent` rows (full history
                                 in the window)
          - avg_rating_current_tier — over rows whose claimed_at is
                                 AFTER tier_set_at (i.e. ratings under
                                 the CURRENT model). This is the
                                 signal Tory should act on; the
                                 all-history average blurs across
                                 tier changes per overseer flag
                                 2026-05-27.

        Plus tier metadata so the caller doesn't have to round-trip.
        """
        claimed_by = f"{agent_type}-agent:{agent_name}"
        rows = self._conn.execute(
            "SELECT id, quality_rating, claimed_at, "
            "       actual_model_used, completed_at "
            "FROM sibling_tasks "
            "WHERE claimed_by = ? AND status = 'completed' "
            "ORDER BY claimed_at DESC LIMIT ?",
            (claimed_by, int(last_n)),
        ).fetchall()
        rated = [dict(r) for r in rows
                 if r["quality_rating"] is not None]
        unrated = [dict(r) for r in rows
                   if r["quality_rating"] is None]
        avg = (sum(r["quality_rating"] for r in rated) / len(rated)
               if rated else None)

        # Tier-aware sub-average: only ratings recorded under the
        # current tier (claimed_at > tier_set_at).
        tier_row = self.get_sub_agent_tier(agent_type, agent_name)
        tier_set_at = (tier_row or {}).get("tier_set_at") or ""
        if tier_set_at:
            current_tier_rated = [
                r for r in rated
                if (r.get("claimed_at") or "") > tier_set_at
            ]
            avg_current = (
                sum(r["quality_rating"] for r in current_tier_rated)
                / len(current_tier_rated)
                if current_tier_rated else None
            )
            current_tier_count = len(current_tier_rated)
        else:
            avg_current = avg
            current_tier_count = len(rated)

        return {
            "recent": [dict(r) for r in rows],
            "n": len(rows),
            "avg_rating": round(avg, 2) if avg is not None else None,
            "avg_rating_current_tier":
                round(avg_current, 2) if avg_current is not None
                else None,
            "current_tier_rated_count": current_tier_count,
            "rated_count": len(rated),
            "unrated_count": len(unrated),
            "tier": (tier_row or {}).get("model_tier"),
            "tier_set_at": tier_set_at,
            "tier_set_by": (tier_row or {}).get("tier_set_by"),
        }

    # ── Phase 1 (2026-05-27): pull_events ───────────────────────
    #
    # Every external drill into the corpus is a refinement signal. The
    # overseer reads aggregate pull_event stats to decide which gist
    # prompts and abstractions need evolving. Recording is best-effort;
    # never let an instrumentation failure block the surface that did
    # the actual pull.

    # caller_class derivation (2026-06-06, looper iter #2 proposal).
    # THE F1 adoption metric is "are organic external AIs actually
    # reading the corpus?" — and that signal was unreadable while
    # looper/bootstrap/verification probes pollute the same surface.
    # caller_class is computed at INSERT time from caller_id; the
    # surface is documented in memory/looper_command.md so future
    # sessions tag correctly.
    @staticmethod
    def classify_caller(caller_id):
        """Derive caller_class from caller_id. NEVER raises."""
        try:
            cid = (caller_id or "").strip().lower()
        except Exception:
            return ""
        if not cid:
            # No caller_id passed → an external session called the MCP
            # tool without identifying itself. Best proxy we have for
            # "organic external AI". The convention is: ALL automation
            # MUST pass a tagged caller_id; everything else is organic.
            return "organic-external"
        # Looper iterations
        if cid.startswith("looper-") or cid.startswith("looper:") \
           or cid.startswith("looper "):
            return "automation:looper"
        # Bootstrap-era probes from the slice 14.7+ ship sequence
        if (cid.startswith("phase1-") or cid.startswith("phase2-")
                or cid.startswith("phase3-") or cid.startswith("setup-")
                or cid.startswith("bootstrap-") or "checkpoint" in cid):
            return "automation:bootstrap"
        # Gateway parity tests + cross-system probes
        if "gateway-" in cid or "parity-probe" in cid:
            return "automation:bootstrap"
        # Tory's manual probes from his own Claude sessions
        if cid.startswith("tory-") or cid.startswith("user-"):
            return "user-probe"
        # Claude Code sessions running scripted F1 verification
        if cid.startswith("claude-code-") and (
                "verify" in cid or "audit" in cid or "regress" in cid
                or "acceptance" in cid or "e2e" in cid
                or "test" in cid):
            return "automation:verification"
        # Other claude-code sessions tagged organically
        if cid.startswith("claude-code-"):
            return "external-tagged"
        # Internal cortex surfaces
        if (cid in ("overseer-chat", "overseer", "internal",
                     "health-check")
                or cid.startswith("overseer:")
                or cid.startswith("internal:")):
            return "internal"
        # Hub UI surfaces
        if cid.startswith("hub:") or "hub-" in cid \
           or cid == "hub":
            return "hub"
        # Anything else has a caller_id we don't recognize — external
        # but identifiable, distinct from the bare-no-id "organic"
        # signal.
        return "external-tagged"

    def record_pull_event(self, *, artifact_table, artifact_id, surface,
                          parent_artifact_table=None,
                          parent_artifact_id=None,
                          query_text=None, caller_id=None):
        """Record one pull event. Returns the new row id or None on
        failure. Designed to be cheap + crash-safe — exceptions are
        swallowed and logged at WARNING so callers can fire-and-forget.

        caller_class is computed at INSERT time from caller_id via
        classify_caller (2026-06-06, looper iter #2 ship).
        """
        caller_class = self.classify_caller(caller_id)
        try:
            cur = self._conn.execute(
                "INSERT INTO pull_events "
                "(artifact_table, artifact_id, surface, "
                "parent_artifact_table, parent_artifact_id, "
                "query_text, caller_id, caller_class) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(artifact_table or ""),
                    int(artifact_id) if artifact_id is not None else 0,
                    str(surface or ""),
                    str(parent_artifact_table) if parent_artifact_table else None,
                    int(parent_artifact_id) if parent_artifact_id else None,
                    str(query_text) if query_text else None,
                    str(caller_id) if caller_id else None,
                    caller_class,
                ),
            )
            self._safe_commit()
            return cur.lastrowid
        except Exception as e:
            log.warning("record_pull_event failed (%s/%s via %s): %s",
                        artifact_table, artifact_id, surface, e)
            return None

    def recent_pull_events(self, *, limit=50, surface=None,
                             artifact_table=None, days=None):
        """List recent pull events. Optional filters narrow by surface
        ('mcp:cortex_search'), artifact_table ('summaries_gist'), or
        time window (days back from now)."""
        # _pull_conn() first: it may lazily attach gateway.db, and the
        # source expression must match the connection's attach state.
        conn = self._pull_conn()
        sql = ("SELECT * FROM {} pe WHERE 1=1"
               .format(self._pull_events_source()))
        params: list = []
        if surface:
            sql += " AND surface = ?"
            params.append(surface)
        if artifact_table:
            sql += " AND artifact_table = ?"
            params.append(artifact_table)
        if days:
            sql += " AND pulled_at >= datetime('now', ?)"
            params.append(f"-{int(days)} days")
        sql += " ORDER BY pulled_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def pull_event_stats(self, *, days=7):
        """Aggregate stats for the overseer to surface in working
        memory and reason about prompt-evolution priority.

        Returns {
          'total': int,
          'by_surface': {surface: count, ...},
          'by_artifact_table': {table: count, ...},
          'by_caller_class': {class: count, ...},
          'organic_external_count': int,    # THE F1 adoption metric
          'automation_count': int,          # everything tagged as auto
          'signal_ratio': float,            # organic / total (0..1)
          'top_pulled': [(artifact_table, artifact_id, count), ...],
          'top_pulled_organic': [(artifact_table, artifact_id, count), ...],
          'window_days': days,
        }
        """
        window_clause = "WHERE pulled_at >= datetime('now', ?)"
        window_param = f"-{int(days)} days"
        # Cloud P2: read over the local+gateway union so connector pulls
        # recorded in the co-located gateway.db count toward F1.
        # ORDER MATTERS: _pull_conn() first — it may lazily attach
        # gateway.db, and _pull_events_source() must see the SAME
        # attached/unattached state the connection has.
        conn = self._pull_conn()
        src = self._pull_events_source()
        total = conn.execute(
            f"SELECT COUNT(*) FROM {src} pe {window_clause}",
            (window_param,),
        ).fetchone()[0]
        if self.gateway_db_attached:
            by_source = {
                r[0]: r[1] for r in conn.execute(
                    f"SELECT source, COUNT(*) FROM {src} pe "
                    f"{window_clause} GROUP BY source",
                    (window_param,),
                ).fetchall()
            }
        else:
            # Unattached reads use the plain table (no source column,
            # keeping the legacy row shape); everything local is core.
            by_source = {"core": int(total)} if total else {}
        by_surface = {
            r[0]: r[1] for r in conn.execute(
                f"SELECT surface, COUNT(*) FROM {src} pe "
                f"{window_clause} GROUP BY surface",
                (window_param,),
            ).fetchall()
        }
        by_table = {
            r[0]: r[1] for r in conn.execute(
                f"SELECT artifact_table, COUNT(*) FROM {src} pe "
                f"{window_clause} GROUP BY artifact_table",
                (window_param,),
            ).fetchall()
        }
        by_caller_class = {
            r[0]: r[1] for r in conn.execute(
                f"SELECT caller_class, COUNT(*) FROM {src} pe "
                f"{window_clause} GROUP BY caller_class",
                (window_param,),
            ).fetchall()
        }
        organic = int(by_caller_class.get("organic-external", 0))
        automation = sum(
            v for k, v in by_caller_class.items()
            if k.startswith("automation:")
        )
        top = [
            (r[0], r[1], r[2]) for r in conn.execute(
                f"SELECT artifact_table, artifact_id, COUNT(*) AS c "
                f"FROM {src} pe {window_clause} "
                f"GROUP BY artifact_table, artifact_id "
                f"ORDER BY c DESC LIMIT 20",
                (window_param,),
            ).fetchall()
        ]
        # Top pulled BY ORGANIC EXTERNAL only — answers "what are
        # real users actually drilling into?" cleanly.
        top_organic = [
            (r[0], r[1], r[2]) for r in conn.execute(
                f"SELECT artifact_table, artifact_id, COUNT(*) AS c "
                f"FROM {src} pe {window_clause} "
                f"  AND caller_class = 'organic-external' "
                f"GROUP BY artifact_table, artifact_id "
                f"ORDER BY c DESC LIMIT 20",
                (window_param,),
            ).fetchall()
        ]
        signal_ratio = round(organic / total, 3) if total else 0.0
        return {
            "total": int(total),
            "by_surface": by_surface,
            "by_artifact_table": by_table,
            "by_caller_class": by_caller_class,
            "organic_external_count": organic,
            "automation_count": int(automation),
            "signal_ratio": signal_ratio,
            "top_pulled": top,
            "top_pulled_organic": top_organic,
            "window_days": int(days),
            # Cloud P2 additions (keys only ADDED, never renamed —
            # deterministic_loop.py parses this shape field-by-field).
            "by_source": by_source,
            "gateway_attached": bool(self.gateway_db_attached),
        }

    # ── Phase 1 (2026-05-27): gist_prompts ──────────────────────
    #
    # Currently a placeholder. The current gist prompt is generated
    # by prompts.session_gist_prompt(*kwargs*), so until the prompt is
    # restructured to support DB-sourced templating, this table is
    # write-rarely / read-rarely. Helpers below let the overseer author
    # v1 when it decides to evolve the prompt and read the active row
    # at that point.

    def get_active_gist_prompt(self):
        """Return the currently-active gist_prompts row or None."""
        row = self._conn.execute(
            "SELECT * FROM gist_prompts WHERE is_active = 1 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def list_gist_prompts(self, *, limit=20):
        rows = self._conn.execute(
            "SELECT * FROM gist_prompts ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_gist_prompt(self, *, version_label, prompt_text,
                        rationale=None, make_active=False):
        """Insert a new gist_prompts version. If make_active=True,
        deprecates the current active row first. Returns the new row id.
        """
        if not version_label or not prompt_text:
            raise ValueError(
                "version_label and prompt_text are required")
        if make_active:
            self._conn.execute(
                "UPDATE gist_prompts SET is_active = 0, "
                "deprecated_at = datetime('now') WHERE is_active = 1"
            )
        cur = self._conn.execute(
            "INSERT INTO gist_prompts "
            "(version_label, prompt_text, is_active, rationale) "
            "VALUES (?, ?, ?, ?)",
            (
                str(version_label),
                str(prompt_text),
                1 if make_active else 0,
                str(rationale) if rationale else None,
            ),
        )
        self._safe_commit()
        return cur.lastrowid

    # ── Slice 5: human_journal_entries ──────────────────────────

    def add_human_journal_entry(self, *, text, entry_type="free",
                                local_created_at=""):
        if not text or not text.strip():
            raise ValueError("text required")
        cur = self._conn.execute(
            "INSERT INTO human_journal_entries "
            "(text, entry_type, local_created_at) VALUES (?, ?, ?)",
            (text.strip(), entry_type, local_created_at),
        )
        self._safe_commit()
        return cur.lastrowid

    def list_human_journal_entries(self, *, limit=100, offset=0):
        rows = self._conn.execute(
            "SELECT * FROM human_journal_entries "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ).fetchall()
        return [dict(r) for r in rows]

    def human_journal_entries_in_window(self, *, start_utc_iso,
                                          end_utc_iso, limit=200):
        """Used by the temporal-narrative gatherers to inject the
        user's own writing for the period being summarized.
        Both bounds are UTC ISO strings; matches the format the
        temporal helpers produce."""
        rows = self._conn.execute(
            "SELECT * FROM human_journal_entries "
            "WHERE created_at >= ? AND created_at < ? "
            "ORDER BY created_at ASC LIMIT ?",
            (start_utc_iso, end_utc_iso, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_human_journal_entry(self, entry_id):
        cur = self._conn.execute(
            "DELETE FROM human_journal_entries WHERE id = ?",
            (int(entry_id),),
        )
        self._safe_commit()
        return cur.rowcount

    # ── Slice 6: people ────────────────────────────────────────

    def get_person_by_name(self, name):
        """Case-insensitive name lookup — used by add to prevent
        duplicate creation when an agent encounters the same person
        across sessions.

        If the matched row has been merged into another (archived via
        merged_into_id by merge_people), resolve to the live survivor so
        callers — add_person idempotency in particular — land on the
        canonical row rather than the archived shell."""
        if not name:
            return None
        row = self._conn.execute(
            "SELECT * FROM overseer_people WHERE LOWER(name) = LOWER(?)",
            (name.strip(),),
        ).fetchone()
        if not row:
            return None
        person = dict(row)
        if person.get("merged_into_id"):
            survivor = self.get_person(person["merged_into_id"])
            if survivor:
                return survivor
        return person

    def get_person(self, person_id):
        row = self._conn.execute(
            "SELECT * FROM overseer_people WHERE id = ?", (int(person_id),)
        ).fetchone()
        return dict(row) if row else None

    def list_people(self, *, limit=200, offset=0,
                     order_by="last_interacted_at"):
        allowed_orders = {
            "last_interacted_at", "updated_at", "created_at", "name",
        }
        if order_by not in allowed_orders:
            order_by = "last_interacted_at"
        try:
            rows = self._conn.execute(
                "SELECT * FROM overseer_people WHERE merged_into_id IS NULL "
                "ORDER BY {} DESC NULLS LAST "
                "LIMIT ? OFFSET ?".format(order_by),
                (int(limit), int(offset)),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = self._conn.execute(
                "SELECT * FROM overseer_people WHERE merged_into_id IS NULL "
                "ORDER BY {} DESC "
                "LIMIT ? OFFSET ?".format(order_by),
                (int(limit), int(offset)),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_people(self, query, *, limit=50):
        """LIKE search across name + display_name + tags + handles +
        expertise + notes. Returns rows ordered by last_interacted."""
        if not query or not query.strip():
            return self.list_people(limit=limit)
        like = "%{}%".format(query.strip())
        rows = self._conn.execute(
            "SELECT * FROM overseer_people WHERE merged_into_id IS NULL AND ("
            "  LOWER(name) LIKE LOWER(?) "
            "  OR LOWER(display_name) LIKE LOWER(?) "
            "  OR LOWER(online_handles_json) LIKE LOWER(?) "
            "  OR LOWER(areas_of_expertise_json) LIKE LOWER(?) "
            "  OR LOWER(tags_json) LIKE LOWER(?) "
            "  OR LOWER(aliases_json) LIKE LOWER(?) "
            "  OR LOWER(notes) LIKE LOWER(?) "
            ") ORDER BY last_interacted_at DESC, updated_at DESC "
            "LIMIT ?",
            (like, like, like, like, like, like, like, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_person(self, *, name, display_name="", online_handles=None,
                    social_links=None, areas_of_expertise=None,
                    notes="", tags=None, aliases=None,
                    last_interacted_at=None,
                    created_by_agent="", created_by_session_id=""):
        """Idempotent on case-insensitive name. Returns dict with
        {person, created} where `created` is True if a new row was
        inserted, False if an existing row matched and was returned
        unchanged (in which case the caller should call update_person
        if they want to merge in new data).
        """
        if not name or not name.strip():
            raise ValueError("name required")
        existing = self.get_person_by_name(name)
        if existing:
            return {"person": existing, "created": False}

        cur = self._conn.execute(
            "INSERT INTO overseer_people (name, display_name, online_handles_json, "
            " social_links_json, areas_of_expertise_json, notes, "
            " tags_json, aliases_json, last_interacted_at, created_by_agent, "
            " created_by_session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name.strip(),
                display_name.strip() if display_name else "",
                json.dumps(online_handles or []),
                json.dumps(social_links or []),
                json.dumps(areas_of_expertise or []),
                notes.strip() if notes else "",
                json.dumps(tags or []),
                json.dumps(aliases or []),
                last_interacted_at,
                created_by_agent or "",
                created_by_session_id or "",
            ),
        )
        self._safe_commit()
        return {"person": self.get_person(cur.lastrowid), "created": True}

    def update_person(self, person_id, *, display_name=None,
                       online_handles=None, social_links=None,
                       areas_of_expertise=None, notes_append=None,
                       notes_replace=None, tags=None, aliases=None,
                       last_interacted_at=None):
        """Update a person row. JSON fields (handles/links/expertise/
        tags) are REPLACE-mode by default — agent passes the full
        new list. Notes have two modes:
          - notes_append: appends '\\n\\n[ts agent] <text>' to existing
                          notes (audit-trailed). The default for agent
                          updates so we don't overwrite prior notes.
          - notes_replace: replaces notes entirely. For manual UI edits.
        last_interacted_at: updatable but the system has NO nudge
        logic that reads it — it exists for chronological ordering only.
        """
        existing = self.get_person(person_id)
        if not existing:
            return None
        sets = ["updated_at = datetime('now')"]
        params: list = []
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(display_name)
        if online_handles is not None:
            sets.append("online_handles_json = ?")
            params.append(json.dumps(online_handles))
        if social_links is not None:
            sets.append("social_links_json = ?")
            params.append(json.dumps(social_links))
        if areas_of_expertise is not None:
            sets.append("areas_of_expertise_json = ?")
            params.append(json.dumps(areas_of_expertise))
        if tags is not None:
            sets.append("tags_json = ?")
            params.append(json.dumps(tags))
        if aliases is not None:
            sets.append("aliases_json = ?")
            params.append(json.dumps(aliases))
        if last_interacted_at is not None:
            sets.append("last_interacted_at = ?")
            params.append(last_interacted_at)
        if notes_replace is not None:
            sets.append("notes = ?")
            params.append(notes_replace)
        elif notes_append is not None and notes_append.strip():
            stamp = self._conn.execute(
                "SELECT datetime('now')").fetchone()[0]
            old = existing.get("notes") or ""
            sep = "\n\n" if old else ""
            new_notes = "{}{}[{}] {}".format(
                old, sep, stamp, notes_append.strip(),
            )
            sets.append("notes = ?")
            params.append(new_notes)
        params.append(int(person_id))
        self._conn.execute(
            "UPDATE overseer_people SET " + ", ".join(sets) + " WHERE id = ?",
            params,
        )
        self._safe_commit()
        return self.get_person(person_id)

    def delete_person(self, person_id):
        cur = self._conn.execute(
            "DELETE FROM overseer_people WHERE id = ?", (int(person_id),))
        self._safe_commit()
        return cur.rowcount

    # ── person_notes (2026-06-13 taxonomy build) ──────────────────────
    # Structured, queryable notes ABOUT a person, each carrying the
    # integrity pair (provenance = who authored, modality = claim type)
    # plus a lens-ish note_kind and a supersession edge. The person's
    # free-form `notes` blob stays for back-compat; this is the channel
    # Tory adds context through and external AIs query along axes.

    def get_person_note(self, note_id):
        row = self._conn.execute(
            "SELECT * FROM person_notes WHERE id = ?", (int(note_id),)
        ).fetchone()
        return dict(row) if row else None

    def add_person_note(self, person_id, *, body, provenance="overseer",
                        modality="statement", note_kind="context",
                        created_by_agent="", created_by_session_id="",
                        local_created_at=None):
        """Append a structured note about a person. Returns the new note
        dict, or None if the person doesn't exist."""
        if not self.get_person(person_id):
            return None
        if not body or not body.strip():
            raise ValueError("body required")
        cur = self._conn.execute(
            "INSERT INTO person_notes (person_id, body, provenance, "
            " modality, note_kind, created_by_agent, "
            " created_by_session_id, local_created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(person_id), body.strip(), provenance or "overseer",
             modality or "statement", note_kind or "context",
             created_by_agent or "", created_by_session_id or "",
             local_created_at),
        )
        # Touch the person so last-edited ordering stays meaningful.
        self._conn.execute(
            "UPDATE overseer_people SET updated_at = datetime('now') "
            "WHERE id = ?", (int(person_id),))
        self._safe_commit()
        return self.get_person_note(cur.lastrowid)

    def list_person_notes(self, person_id, *, include_superseded=False,
                          limit=200):
        sql = "SELECT * FROM person_notes WHERE person_id = ? "
        if not include_superseded:
            sql += "AND superseded_by IS NULL "
        sql += "ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(
            sql, (int(person_id), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def supersede_person_note(self, old_note_id, new_note_id):
        """Mark old_note as superseded by new_note (stance/supersession
        edge). Representation-improvement keeps the old as history."""
        self._conn.execute(
            "UPDATE person_notes SET superseded_by = ? WHERE id = ?",
            (int(new_note_id), int(old_note_id)))
        self._safe_commit()
        return self.get_person_note(old_note_id)

    def delete_person_note(self, note_id):
        cur = self._conn.execute(
            "DELETE FROM person_notes WHERE id = ?", (int(note_id),))
        self._safe_commit()
        return cur.rowcount

    # ── Tech skills + rules (2026-07-12) ────────────────────────
    # Living skills portfolio + tech-decisions rules log. Written by
    # connected AI agents (MCP), read by every AI at session start
    # via /intro. Mirrors the people-entity patterns: idempotent
    # upserts on case-insensitive natural keys, audit source fields.

    SKILL_LOG_KINDS = ("lesson", "win", "project", "tooling", "note")

    def _get_skill_by_name(self, name):
        row = self._conn.execute(
            "SELECT * FROM tech_skills WHERE LOWER(name) = LOWER(?)",
            (name.strip(),)).fetchone()
        return dict(row) if row else None

    def upsert_skill(self, *, name, proficiency=None, summary=None,
                     tools=None):
        """Idempotent on case-insensitive name. Creates the skill or
        refines it: only non-empty provided fields overwrite (refine
        rule; an explicit "" cannot blank a living portfolio field).
        Serialized under the write lock; a concurrent same-name
        insert converges via the NOCASE UNIQUE constraint.
        Returns {skill, created}."""
        if not name or not name.strip():
            raise ValueError("name required")
        with self._write_lock:
            existing = self._get_skill_by_name(name)
            if existing:
                sets, params = ["updated_at = datetime('now')"], []
                for col, val in (("proficiency", proficiency),
                                 ("summary", summary), ("tools", tools)):
                    if val is not None and str(val).strip():
                        sets.append(f"{col} = ?")
                        params.append(str(val).strip())
                if len(sets) > 1:
                    params.append(existing["id"])
                    self._conn.execute(
                        "UPDATE tech_skills SET " + ", ".join(sets)
                        + " WHERE id = ?", params)
                    self._safe_commit()
                    existing = self._get_skill_by_name(name)
                return {"skill": existing, "created": False}
            try:
                self._conn.execute(
                    "INSERT INTO tech_skills (name, proficiency, summary, tools) "
                    "VALUES (?, ?, ?, ?)",
                    (name.strip(), (proficiency or "").strip(),
                     (summary or "").strip(), (tools or "").strip()))
                self._safe_commit()
                return {"skill": self._get_skill_by_name(name),
                        "created": True}
            except sqlite3.IntegrityError:
                # Lost a cross-process race; the row exists now.
                return {"skill": self._get_skill_by_name(name),
                        "created": False}

    def log_skill_entry(self, *, skill, kind="note", content,
                        project="", source="", proficiency=None):
        """Append a log entry under a skill (created if new). kind is
        case-normalized; anything outside SKILL_LOG_KINDS is coerced
        to 'note'. proficiency, if given, also updates the skill
        header. Returns {skill, entry, skill_created}."""
        if not content or not content.strip():
            raise ValueError("content required")
        r = self.upsert_skill(name=skill, proficiency=proficiency)
        kind = (kind or "note").strip().lower()
        kind = kind if kind in self.SKILL_LOG_KINDS else "note"
        cur = self._conn.execute(
            "INSERT INTO tech_skill_log (skill_id, kind, content, "
            " project, source) VALUES (?, ?, ?, ?, ?)",
            (r["skill"]["id"], kind, content.strip(),
             (project or "").strip(), (source or "").strip()))
        self._conn.execute(
            "UPDATE tech_skills SET updated_at = datetime('now') "
            "WHERE id = ?", (r["skill"]["id"],))
        self._safe_commit()
        entry = dict(self._conn.execute(
            "SELECT * FROM tech_skill_log WHERE id = ?",
            (cur.lastrowid,)).fetchone())
        return {"skill": self._get_skill_by_name(skill),
                "entry": entry, "skill_created": r["created"]}

    def list_skills(self, *, limit=100):
        """Portfolio index: headers + entry counts + last activity."""
        rows = self._conn.execute(
            "SELECT s.*, "
            " (SELECT COUNT(*) FROM tech_skill_log l "
            "   WHERE l.skill_id = s.id) AS entry_count, "
            " (SELECT MAX(created_at) FROM tech_skill_log l "
            "   WHERE l.skill_id = s.id) AS last_entry_at "
            "FROM tech_skills s ORDER BY s.updated_at DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def get_skill(self, name, *, log_limit=50):
        """Full portfolio entry: header + recent log, newest first."""
        skill = self._get_skill_by_name(name)
        if not skill:
            return None
        rows = self._conn.execute(
            "SELECT * FROM tech_skill_log WHERE skill_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (skill["id"], int(log_limit))).fetchall()
        skill["log"] = [dict(r) for r in rows]
        return skill

    def add_rule(self, *, title, rule, stack="", situation="",
                 went_wrong="", what_changed="", rationale="",
                 status=None, source=""):
        """Upsert on case-insensitive title so agents can refine or
        retire a rule under its natural key. On update, only non-empty
        fields overwrite (status only when explicitly given). An
        invalid status raises rather than silently keeping the rule
        active. Returns {rule, created}."""
        if not title or not title.strip():
            raise ValueError("title required")
        if status is not None and str(status).strip():
            status = str(status).strip().lower()
            if status not in ("active", "retired"):
                raise ValueError(
                    "status must be 'active' or 'retired', got "
                    f"'{status}'")
        else:
            status = None
        with self._write_lock:
            existing = self._conn.execute(
                "SELECT * FROM tech_rules WHERE LOWER(title) = LOWER(?)",
                (title.strip(),)).fetchone()
            if existing:
                sets, params = ["updated_at = datetime('now')"], []
                for col, val in (("rule", rule), ("stack", stack),
                                 ("situation", situation),
                                 ("went_wrong", went_wrong),
                                 ("what_changed", what_changed),
                                 ("rationale", rationale),
                                 ("source", source)):
                    if val and str(val).strip():
                        sets.append(f"{col} = ?")
                        params.append(str(val).strip())
                if status is not None:
                    sets.append("status = ?")
                    params.append(status)
                params.append(existing["id"])
                self._conn.execute(
                    "UPDATE tech_rules SET " + ", ".join(sets)
                    + " WHERE id = ?", params)
                self._safe_commit()
                row = self._conn.execute(
                    "SELECT * FROM tech_rules WHERE id = ?",
                    (existing["id"],)).fetchone()
                return {"rule": dict(row), "created": False}
            if not rule or not rule.strip():
                raise ValueError("rule required")
            try:
                cur = self._conn.execute(
                    "INSERT INTO tech_rules (title, rule, stack, situation, "
                    " went_wrong, what_changed, rationale, status, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (title.strip(), rule.strip(), (stack or "").strip(),
                     (situation or "").strip(), (went_wrong or "").strip(),
                     (what_changed or "").strip(), (rationale or "").strip(),
                     status or "active", (source or "").strip()))
                self._safe_commit()
            except sqlite3.IntegrityError:
                # Lost a cross-process race; refine the winner instead.
                return self.add_rule(
                    title=title, rule=rule, stack=stack,
                    situation=situation, went_wrong=went_wrong,
                    what_changed=what_changed, rationale=rationale,
                    status=status, source=source)
            row = self._conn.execute(
                "SELECT * FROM tech_rules WHERE id = ?",
                (cur.lastrowid,)).fetchone()
            return {"rule": dict(row), "created": True}

    def list_rules(self, *, status="active", stack=None, limit=200):
        """Rules log, most recently touched first. status=None or
        'all' returns everything; stack substring-filters the tag."""
        sql = "SELECT * FROM tech_rules WHERE 1=1"
        params: list = []
        if status and status != "all":
            sql += " AND status = ?"
            params.append(status)
        if stack:
            sql += " AND LOWER(stack) LIKE ?"
            params.append(f"%{stack.strip().lower()}%")
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def rules_digest(self, *, limit=30):
        """Compact active-rules list for /intro and chat injection:
        [{title, rule, stack}], most recently affirmed first. Fields
        are clamped like every other intro section so an over-long
        (or abusive) agent-written rule cannot flood the brief served
        to every connecting AI; full text stays behind list_rules."""
        rows = self._conn.execute(
            "SELECT title, rule, stack FROM tech_rules "
            "WHERE status = 'active' "
            "ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"title": (r["title"] or "")[:120],
                 "rule": (r["rule"] or "")[:280],
                 "stack": (r["stack"] or "")[:60]} for r in rows]

    # ── notification classification + ambient observations (2026-06-13) ──

    def unclassified_notifications(self, limit=300):
        """device_notifications rows with no classification row yet
        (anchor-mark via LEFT JOIN)."""
        try:
            rows = self._conn.execute(
                "SELECT d.* FROM device_notifications d "
                "LEFT JOIN notification_classification c "
                "  ON c.notification_id = d.id "
                "WHERE c.notification_id IS NULL "
                "ORDER BY d.id LIMIT ?", (int(limit),)).fetchall()
        except sqlite3.OperationalError:
            # device_notifications may not exist on installs without the
            # sync plugin / phone capture yet.
            return []
        return [dict(r) for r in rows]

    def record_notification_classification(self, notification_id, tier,
                                           category, *, app="",
                                           local_classified_at=None):
        self._conn.execute(
            "INSERT OR REPLACE INTO notification_classification "
            "(notification_id, tier, category, app, local_classified_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(notification_id), tier, category, app or "",
             local_classified_at))
        self._safe_commit()

    def add_ambient_observation(self, *, temp_f, location, observed_at,
                                local_observed_at=None, source="phone-widget",
                                raw_notification_id=None, kind="temperature"):
        cur = self._conn.execute(
            "INSERT INTO ambient_observations "
            "(kind, temp_f, location, observed_at, local_observed_at, "
            " source, raw_notification_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, temp_f, location, observed_at, local_observed_at,
             source, raw_notification_id))
        self._safe_commit()
        return cur.lastrowid

    def notification_tier_counts(self):
        """{tier: count} over all classified notifications."""
        try:
            return {r["tier"]: r["c"] for r in self._conn.execute(
                "SELECT tier, COUNT(*) c FROM notification_classification "
                "GROUP BY tier")}
        except sqlite3.OperationalError:
            return {}

    def notification_is_recent_duplicate(self, row_id, app, title, body,
                                         posted_at, *, window_hours=24):
        """True when an identical (app, title, body) notification was
        already posted within the last `window_hours`. Media controls,
        Phone Link heartbeats, and persistent notifications re-post the
        same content dozens of times a day; the repeats carry no new
        information. First occurrence stays signal, repeats get tiered
        drop/duplicate (2026-07-09 cleaning pass). id breaks same-second
        ties so exactly one of an identical pair survives."""
        try:
            row = self._conn.execute(
                "SELECT 1 FROM device_notifications "
                "WHERE app = ? AND title = ? AND body = ? "
                "  AND (posted_at < ? OR (posted_at = ? AND id < ?)) "
                "  AND posted_at >= datetime(?, ?) "
                "LIMIT 1",
                (app or "", title or "", body or "", posted_at, posted_at,
                 int(row_id), posted_at,
                 "-{} hours".format(int(window_hours)))).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def backfill_notification_duplicates(self, *, window_hours=24):
        """Full, idempotent recompute of duplicate marks over ALL
        classified notifications. For each row (posted_at order): if an
        identical row precedes it inside the window it becomes
        drop/duplicate; otherwise a row previously marked duplicate is
        restored to its rule-based tier. Deterministic; safe to re-run.
        Returns {'marked': n, 'restored': m}."""
        import notification_classify as _nc
        marked = restored = 0
        rows = self._conn.execute(
            "SELECT d.id, d.app, d.title, d.body, d.posted_at, "
            "       c.tier, c.category "
            "FROM device_notifications d "
            "JOIN notification_classification c "
            "  ON c.notification_id = d.id "
            "ORDER BY d.posted_at, d.id").fetchall()
        for r in rows:
            is_dup = self.notification_is_recent_duplicate(
                r["id"], r["app"], r["title"], r["body"], r["posted_at"],
                window_hours=window_hours)
            if is_dup and r["category"] != "duplicate":
                self._conn.execute(
                    "UPDATE notification_classification "
                    "SET tier = 'drop', category = 'duplicate' "
                    "WHERE notification_id = ?", (r["id"],))
                marked += 1
            elif not is_dup and r["category"] == "duplicate":
                tier, category = _nc.classify(
                    r["app"], r["title"] or "", r["body"] or "")
                self._conn.execute(
                    "UPDATE notification_classification "
                    "SET tier = ?, category = ? WHERE notification_id = ?",
                    (tier, category, r["id"]))
                restored += 1
        self._safe_commit()
        return {"marked": marked, "restored": restored}

    def recent_ambient(self, *, kind="temperature", limit=200):
        try:
            rows = self._conn.execute(
                "SELECT * FROM ambient_observations WHERE kind = ? "
                "ORDER BY observed_at DESC LIMIT ?",
                (kind, int(limit))).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def merge_people(self, from_id, into_id, *, dry_run=False,
                      agent="looper"):
        """Merge person `from_id` (the duplicate/loser) INTO `into_id`
        (the canonical survivor).

        Re-points references (project_people, phone_contacts) from the
        loser to the survivor, unions the JSON list fields
        (handles/links/expertise/tags), appends the loser's notes to the
        survivor with a merge marker, and ARCHIVES the loser by setting
        merged_into_id + is_provisional=1. The loser row is NEVER
        deleted — audit trail + the looper's no-DELETE policy.

        Returns a plan dict. When dry_run=True, computes the plan and
        makes NO writes (the required dry-run before any real merge).
        """
        from_id = int(from_id)
        into_id = int(into_id)
        if from_id == into_id:
            return {"ok": False, "error": "from_id and into_id are equal"}
        loser = self.get_person(from_id)
        survivor = self.get_person(into_id)
        if not loser:
            return {"ok": False, "error": "from_id %d not found" % from_id}
        if not survivor:
            return {"ok": False, "error": "into_id %d not found" % into_id}
        if loser.get("merged_into_id"):
            return {"ok": False,
                    "error": "from_id %d already merged into %s"
                    % (from_id, loser["merged_into_id"])}
        if survivor.get("merged_into_id"):
            return {"ok": False,
                    "error": "into_id %d is itself merged into %s"
                    % (into_id, survivor["merged_into_id"])}

        def _loads(s):
            try:
                return json.loads(s) if s else []
            except Exception:
                return []

        def _union(a, b):
            out = list(a)
            for x in b:
                if x not in out:
                    out.append(x)
            return out

        pp = self._conn.execute(
            "SELECT id, project FROM project_people WHERE person_id = ?",
            (from_id,)).fetchall()
        pc = self._conn.execute(
            "SELECT id FROM phone_contacts WHERE person_id = ?",
            (from_id,)).fetchall()

        handles = _union(_loads(survivor.get("online_handles_json")),
                         _loads(loser.get("online_handles_json")))
        social = _union(_loads(survivor.get("social_links_json")),
                        _loads(loser.get("social_links_json")))
        expert = _union(_loads(survivor.get("areas_of_expertise_json")),
                        _loads(loser.get("areas_of_expertise_json")))
        tags = _union(_loads(survivor.get("tags_json")),
                      _loads(loser.get("tags_json")))

        plan = {
            "ok": True,
            "dry_run": bool(dry_run),
            "from": {"id": from_id, "name": loser["name"]},
            "into": {"id": into_id, "name": survivor["name"]},
            "project_people_to_move": len(pp),
            "phone_contacts_to_move": len(pc),
            "tags_after": tags,
            "handles_after": handles,
            "expertise_after": expert,
        }
        if dry_run:
            return plan

        stamp = self._conn.execute("SELECT datetime('now')").fetchone()[0]
        # Re-point phone_contacts (keyed by phone_number — no person
        # UNIQUE conflict possible).
        self._conn.execute(
            "UPDATE phone_contacts SET person_id = ?, "
            "updated_at = datetime('now') WHERE person_id = ?",
            (into_id, from_id))
        # Re-point project_people, respecting UNIQUE(project, person_id):
        # if the survivor is already linked to that project, leave the
        # loser's link in place (loser is archived) rather than DELETE it.
        for row in pp:
            dup = self._conn.execute(
                "SELECT 1 FROM project_people WHERE project = ? "
                "AND person_id = ?", (row["project"], into_id)).fetchone()
            if dup:
                continue
            self._conn.execute(
                "UPDATE project_people SET person_id = ? WHERE id = ?",
                (into_id, row["id"]))
        # Union JSON list fields + append the loser's notes onto survivor.
        merge_line = ("[{} merge:{}] Merged from id {} ('{}')."
                      .format(stamp, agent, from_id, loser["name"]))
        loser_notes = (loser.get("notes") or "").strip()
        if loser_notes:
            merge_line += "\nNotes from merged row:\n" + loser_notes
        surv_notes = survivor.get("notes") or ""
        sep = "\n\n" if surv_notes else ""
        self._conn.execute(
            "UPDATE overseer_people SET online_handles_json = ?, "
            "social_links_json = ?, areas_of_expertise_json = ?, "
            "tags_json = ?, notes = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (json.dumps(handles), json.dumps(social), json.dumps(expert),
             json.dumps(tags), surv_notes + sep + merge_line, into_id))
        # Archive the loser (no DELETE).
        tomb = loser.get("notes") or ""
        tsep = "\n\n" if tomb else ""
        tomb_line = ("[{} merge:{}] Merged INTO id {} ('{}') and archived "
                     "(not deleted)."
                     .format(stamp, agent, into_id, survivor["name"]))
        self._conn.execute(
            "UPDATE overseer_people SET merged_into_id = ?, "
            "is_provisional = 1, notes = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (into_id, tomb + tsep + tomb_line, from_id))
        self._safe_commit()

        plan["executed"] = True
        plan["survivor"] = self.get_person(into_id)
        return plan

    # project_people junction

    def link_project_person(self, *, project, person_id, role="",
                              created_by_agent=""):
        """Idempotent — UNIQUE(project, person_id) prevents dupes.
        Returns the new (or existing) link row."""
        if not project or not person_id:
            raise ValueError("project + person_id required")
        try:
            cur = self._conn.execute(
                "INSERT INTO project_people (project, person_id, role, "
                " created_by_agent) VALUES (?, ?, ?, ?)",
                (project.strip(), int(person_id), role or "",
                 created_by_agent or ""),
            )
            self._safe_commit()
            link_id = cur.lastrowid
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT id FROM project_people "
                "WHERE project = ? AND person_id = ?",
                (project.strip(), int(person_id)),
            ).fetchone()
            link_id = row["id"] if row else None
            # If role provided, update it on the existing link
            if role:
                self._conn.execute(
                    "UPDATE project_people SET role = ? WHERE id = ?",
                    (role, link_id))
                self._safe_commit()
        row = self._conn.execute(
            "SELECT * FROM project_people WHERE id = ?", (link_id,)
        ).fetchone()
        return dict(row) if row else None

    def unlink_project_person(self, *, project, person_id):
        cur = self._conn.execute(
            "DELETE FROM project_people "
            "WHERE project = ? AND person_id = ?",
            (project.strip(), int(person_id)),
        )
        self._safe_commit()
        return cur.rowcount

    def people_for_project(self, project):
        """All people linked to a project, with their full row + role."""
        rows = self._conn.execute(
            "SELECT p.*, pp.role, pp.created_at AS link_created_at, "
            "       pp.created_by_agent AS link_created_by_agent "
            "FROM overseer_people p "
            "JOIN project_people pp ON pp.person_id = p.id "
            "WHERE pp.project = ? "
            "ORDER BY p.last_interacted_at DESC NULLS LAST",
            (project.strip(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def projects_for_person(self, person_id):
        """All project links for a person."""
        rows = self._conn.execute(
            "SELECT * FROM project_people WHERE person_id = ? "
            "ORDER BY created_at DESC",
            (int(person_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Slice 5.5: journal cadence guards ──────────────────────

    def journal_count_since(self, since_utc_iso):
        """Return count of journal entries with written_at >= bound."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM overseer_journal "
            "WHERE written_at >= ?",
            (since_utc_iso,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def last_journal_written_at(self):
        """Return the most-recent overseer_journal.written_at (UTC ISO),
        or None if the table is empty."""
        row = self._conn.execute(
            "SELECT MAX(written_at) AS mx FROM overseer_journal"
        ).fetchone()
        return row["mx"] if row else None

    def people_stats(self):
        """Lightweight cross-cutting stats for the people surface.

        Returns a dict with:
          total_people
          added_24h, added_7d
          orphans_count          — people with zero project links
          multi_project_count    — linked to ≥2 projects (connectors)
          top_projects           — list of {project, person_count}
                                   sorted desc, top 10
          top_expertise_tags     — list of {tag, count} sorted desc, top 5
                                   (extracted from areas_of_expertise_json)
          recent_additions       — list of {id, name, created_at,
                                   created_by_agent, created_by_session_id}
                                   newest 10 (for the "what got captured
                                   recently" curation prompt)

        Skipped (intentionally — keep this signal-dense):
          per-agent breakdown (already audit-trailed; queryable via list)
          notes-length stats (fluffy)
        """
        cur = self._conn

        # merged_into_id IS NULL across these counts so archived (merged)
        # rows don't inflate the people-surface stats post-merge.
        total = cur.execute(
            "SELECT COUNT(*) AS n FROM overseer_people "
            "WHERE merged_into_id IS NULL"
        ).fetchone()["n"]

        added_24h = cur.execute(
            "SELECT COUNT(*) AS n FROM overseer_people "
            "WHERE merged_into_id IS NULL "
            "  AND created_at >= datetime('now', '-1 day')"
        ).fetchone()["n"]
        added_7d = cur.execute(
            "SELECT COUNT(*) AS n FROM overseer_people "
            "WHERE merged_into_id IS NULL "
            "  AND created_at >= datetime('now', '-7 days')"
        ).fetchone()["n"]

        orphans = cur.execute(
            "SELECT COUNT(*) AS n FROM overseer_people p "
            "WHERE p.merged_into_id IS NULL AND NOT EXISTS ("
            "  SELECT 1 FROM project_people pp "
            "  WHERE pp.person_id = p.id"
            ")"
        ).fetchone()["n"]

        multi_project = cur.execute(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT person_id FROM project_people "
            "  GROUP BY person_id HAVING COUNT(*) >= 2"
            ")"
        ).fetchone()["n"]

        top_projects_rows = cur.execute(
            "SELECT project, COUNT(*) AS person_count FROM project_people "
            "GROUP BY project ORDER BY person_count DESC LIMIT 10"
        ).fetchall()
        top_projects = [dict(r) for r in top_projects_rows]

        # Expertise tag aggregation — JSON arrays, so we have to
        # deserialize each row. Cap at the most recent 500 people to
        # keep this cheap (the long-tail people aren't relevant for
        # "what kinds of expertise are showing up").
        expertise_counter: dict = {}
        for r in cur.execute(
            "SELECT areas_of_expertise_json FROM overseer_people "
            "WHERE merged_into_id IS NULL "
            "ORDER BY updated_at DESC LIMIT 500"
        ).fetchall():
            try:
                tags = json.loads(r["areas_of_expertise_json"] or "[]")
            except Exception:
                continue
            for t in tags:
                t = (t or "").strip()
                if t:
                    expertise_counter[t] = expertise_counter.get(t, 0) + 1
        top_expertise_tags = sorted(
            ({"tag": t, "count": c} for t, c in expertise_counter.items()),
            key=lambda d: -d["count"],
        )[:5]

        recent_rows = cur.execute(
            "SELECT id, name, created_at, created_by_agent, "
            "       created_by_session_id "
            "FROM overseer_people WHERE merged_into_id IS NULL "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        recent_additions = [dict(r) for r in recent_rows]

        return {
            "total_people": total,
            "added_24h": added_24h,
            "added_7d": added_7d,
            "orphans_count": orphans,
            "multi_project_count": multi_project,
            "top_projects": top_projects,
            "top_expertise_tags": top_expertise_tags,
            "recent_additions": recent_additions,
        }

    # ── Slice 9.3: sibling task dispatch ────────────────────────────
    # Public methods used by the Pi endpoints + the overseer's
    # dispatch_sibling chat tool. All writes go through these so the
    # daily dispatch cap + audit fields are enforced in one place.

    def _local_day_start_iso(self) -> str:
        """ISO timestamp of midnight in the OWNER's TZ, expressed as
        UTC. Used by sibling_* methods so the daily cap calendar
        matches the user's day (same convention as Slice 5.5's
        DailyBudget reset).

        Tenant-TZ pass (cloud P2, 2026-07-20): keyed on
        temporal.tenant_tz() (CORTEX_TENANT_TZ, host-local fallback),
        replacing the manual local_tz_offset_minutes state key, which
        was a fixed offset that broke on DST and defaulted to 0 (=UTC
        midnight) when unset."""
        from datetime import datetime, timezone
        from temporal import tenant_tz
        tz = tenant_tz()
        now_local = (datetime.now(tz) if tz is not None
                     else datetime.now(timezone.utc).astimezone())
        day_start_local = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_local.astimezone(timezone.utc)
        return day_start_utc.strftime("%Y-%m-%d %H:%M:%S")

    def sibling_dispatch(self, *, prompt, created_by="overseer",
                         target="claude-code", task_type="judgment",
                         preferred_model_tier="smart",
                         cost_budget_usd=0.50, context=None,
                         daily_cap=20) -> dict:
        """Create a sibling task. Returns {ok, id, ...} or {ok: False, error}.

        Enforces the daily dispatch cap (passed in by the Pi endpoint
        layer, which reads ``loop_daily_sibling_dispatches`` from
        plugin.toml — default 20). Measured by local-day rollover so it
        matches the LLM-budget calendar (Slice 5.5 alignment).

        ``context`` is any dict (excerpts, refs, current overseer state);
        serialized to JSON for storage."""
        import json as _json
        day_start_iso = self._local_day_start_iso()

        used_today = self._conn.execute(
            "SELECT COUNT(*) AS n FROM sibling_tasks "
            "WHERE created_by = ? AND created_at >= ?",
            (created_by, day_start_iso),
        ).fetchone()["n"]
        if used_today >= daily_cap:
            return {
                "ok": False,
                "error": (f"daily dispatch cap reached "
                          f"({used_today}/{daily_cap}); "
                          f"resets at next local midnight"),
                "cap": daily_cap,
                "used_today": used_today,
            }

        ctx_json = _json.dumps(context or {}, default=str, ensure_ascii=False)
        cur = self._conn.execute(
            "INSERT INTO sibling_tasks "
            "  (created_by, target, prompt, context_json, cost_budget_usd, "
            "   task_type, preferred_model_tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (created_by, target, prompt, ctx_json,
             float(cost_budget_usd), task_type, preferred_model_tier),
        )
        self._safe_commit()
        return {
            "ok": True,
            "id": cur.lastrowid,
            "used_today": used_today + 1,
            "cap": daily_cap,
        }

    def sibling_pending(self, *, target=None, limit=50) -> list[dict]:
        """List tasks a sibling can claim. Filters out claimed/done."""
        sql = ("SELECT id, created_at, created_by, target, prompt, "
               "       context_json, cost_budget_usd, task_type, "
               "       preferred_model_tier "
               "FROM sibling_tasks WHERE status = 'pending'")
        params: list = []
        if target:
            sql += " AND (target = ? OR target = 'any')"
            params.append(target)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def sibling_claim(self, task_id, *, claimed_by) -> dict:
        """Atomic claim. Refuses if already claimed/completed."""
        cur = self._conn.execute(
            "UPDATE sibling_tasks SET status = 'claimed', "
            "  claimed_at = datetime('now'), claimed_by = ? "
            "WHERE id = ? AND status = 'pending'",
            (claimed_by, int(task_id)),
        )
        self._safe_commit()
        if cur.rowcount == 0:
            row = self._conn.execute(
                "SELECT status, claimed_by FROM sibling_tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "no such task"}
            return {
                "ok": False,
                "error": (f"task already {row['status']}"
                          + (f" by {row['claimed_by']}"
                             if row["claimed_by"] else "")),
            }
        full = self._conn.execute(
            "SELECT * FROM sibling_tasks WHERE id = ?",
            (int(task_id),),
        ).fetchone()
        return {"ok": True, "task": dict(full) if full else None}

    def sibling_complete(self, task_id, *, result_text,
                         actual_model_used="",
                         result_cost_usd=0.0,
                         dispatch_quality_rating=None,
                         dispatch_quality_notes="") -> dict:
        """Submit a completed result + optional reciprocal grade of
        the dispatch."""
        cur = self._conn.execute(
            "UPDATE sibling_tasks SET status = 'completed', "
            "  completed_at = datetime('now'), result_text = ?, "
            "  actual_model_used = ?, result_cost_usd = ?, "
            "  dispatch_quality_rating = ?, dispatch_quality_notes = ? "
            "WHERE id = ? AND status = 'claimed'",
            (result_text, actual_model_used,
             float(result_cost_usd),
             (int(dispatch_quality_rating)
              if dispatch_quality_rating is not None else None),
             dispatch_quality_notes,
             int(task_id)),
        )
        self._safe_commit()
        if cur.rowcount == 0:
            row = self._conn.execute(
                "SELECT status FROM sibling_tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            return {
                "ok": False,
                "error": (f"cannot complete: task is "
                          f"{row['status'] if row else 'missing'}, "
                          f"expected 'claimed'"),
            }
        return {"ok": True, "id": int(task_id)}

    def sibling_reject(self, task_id, *, reason) -> dict:
        """Mark a task as rejected (sibling chose not to do it).
        Different from failed (sibling tried and couldn't)."""
        cur = self._conn.execute(
            "UPDATE sibling_tasks SET status = 'rejected', "
            "  rejection_reason = ?, completed_at = datetime('now') "
            "WHERE id = ? AND status IN ('pending', 'claimed')",
            (reason, int(task_id)),
        )
        self._safe_commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "task not in rejectable state"}
        return {"ok": True, "id": int(task_id)}

    def sibling_recent_completed(self, *, limit=20,
                                 unread_to_overseer_only=False) -> list[dict]:
        """List recently completed tasks. Used by the overseer's tick
        loop to find new results to integrate, and by the chat context
        builder to surface unread results in the freshness section."""
        sql = ("SELECT * FROM sibling_tasks "
               "WHERE status IN ('completed', 'failed', 'rejected')")
        if unread_to_overseer_only:
            sql += " AND (quality_rating IS NULL OR quality_rating = 0)"
        sql += " ORDER BY completed_at DESC LIMIT ?"
        rows = self._conn.execute(sql, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def sibling_rate_result(self, task_id, *, rating, notes="",
                            dataset_candidate=False) -> dict:
        """Overseer rates a sibling's completed result (next tick).
        Optionally flags the (prompt, context, result) triple as a
        training-data candidate for future Category C agents."""
        cur = self._conn.execute(
            "UPDATE sibling_tasks SET quality_rating = ?, "
            "  quality_notes = ?, dataset_candidate = ? "
            "WHERE id = ? AND status = 'completed'",
            (int(rating), notes, 1 if dataset_candidate else 0,
             int(task_id)),
        )
        self._safe_commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "no completed task with that id"}
        return {"ok": True, "id": int(task_id)}

    def sibling_dispatch_stats(self, *, daily_cap=20) -> dict:
        """Headline counts + daily budget used. Surfaces in the
        chat freshness section so the overseer sees its own
        dispatch posture. ``daily_cap`` passed by Pi endpoint layer
        from plugin.toml.

        Slice 9.2.1 (2026-05-16): added ``unrated_count`` and
        ``pending_for_me`` so the overseer's freshness block shows
        the read-side of its own dispatch posture (\"are there
        completed tasks I owe a rating to; are there dispatches
        still in flight\"), not just the write-side counter.
        Per overseer's explicit ask: \"the loop should surface it
        the same way it surfaces ingest queue depth and last-gist
        age.\" A-only — no Category B/C placeholders.
        """
        cur = self._conn
        rows = cur.execute(
            "SELECT status, COUNT(*) AS n FROM sibling_tasks "
            "GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        day_start_iso = self._local_day_start_iso()
        today_n = cur.execute(
            "SELECT COUNT(*) AS n FROM sibling_tasks "
            "WHERE created_by = 'overseer' AND created_at >= ?",
            (day_start_iso,),
        ).fetchone()["n"]
        # Completed tasks the overseer dispatched but hasn't rated
        # yet. Scoped to created_by='overseer' because that's whose
        # audit loop the rating closes — Tory-created test tasks
        # don't appear in the overseer's "unrated" tally.
        unrated_n = cur.execute(
            "SELECT COUNT(*) AS n FROM sibling_tasks "
            "WHERE status = 'completed' "
            "AND quality_rating IS NULL "
            "AND created_by = 'overseer'"
        ).fetchone()["n"]
        # Dispatches still in flight (no sibling has finished yet).
        # Includes both unclaimed (pending) and in-progress (claimed).
        pending_for_me_n = cur.execute(
            "SELECT COUNT(*) AS n FROM sibling_tasks "
            "WHERE created_by = 'overseer' "
            "AND status IN ('pending', 'claimed')"
        ).fetchone()["n"]
        return {
            "by_status": by_status,
            "today_dispatches": today_n,
            "daily_cap": daily_cap,
            "remaining_today": max(0, daily_cap - today_n),
            "unrated_count": unrated_n,
            "pending_for_me": pending_for_me_n,
        }

    # ── Lemon Squeezer dispatch export (2026-06-13) ─────────────────
    # Read-only assembler: completed + rated sibling dispatches in the
    # exact shape Lemon Squeezer's /ingest/dispatches expects (see the
    # Swarm Board dispatch-export contract). METADATA ONLY - no prompt or
    # response text. The desktop connector pulls this and pushes to Lemon;
    # the cursor lives in desktop (Lemon is idempotent on dispatch_id), so
    # Core stays stateless - no exported_at column, no ack handshake.

    @staticmethod
    def _dispatch_latency_ms(claimed_at, completed_at):
        """A-tier wall-clock ms (completed - claimed); None if unparseable."""
        if not claimed_at or not completed_at:
            return None
        import datetime as _dt

        def _p(s):
            try:
                return _dt.datetime.fromisoformat(
                    str(s).replace("Z", "+00:00"))
            except Exception:
                return None
        a, b = _p(claimed_at), _p(completed_at)
        if a is None or b is None:
            return None
        ms = int((b - a).total_seconds() * 1000)
        return ms if ms >= 0 else None

    def graded_dispatches_for_export(self, *, since_id=0, limit=500):
        """Return graded dispatches as Lemon-shaped dicts.

        task_type (the routing tag): the B/C agent name when the target
        encodes one ('b-agent:<name>'/'c-agent:<name>'), else the free-form
        task_type column, else the target. latency_ms: B/C agents carry an
        exact value in b_invocation_transcripts; A-tier siblings get
        completed_at - claimed_at. since_id trims the read once the desktop
        connector has shipped everything up to a high-water mark.
        """
        rows = self._conn.execute(
            "SELECT s.id, s.target, s.task_type, s.actual_model_used, "
            "       s.quality_rating, s.result_cost_usd, "
            "       s.claimed_at, s.completed_at, "
            "       (SELECT t.latency_ms FROM b_invocation_transcripts t "
            "        WHERE t.sibling_task_id = s.id "
            "        ORDER BY t.id DESC LIMIT 1) AS b_latency_ms "
            "FROM sibling_tasks s "
            "WHERE s.status = 'completed' "
            "  AND s.quality_rating IS NOT NULL "
            "  AND s.actual_model_used IS NOT NULL "
            "  AND s.actual_model_used <> '' "
            "  AND CAST(s.id AS INTEGER) > ? "
            "ORDER BY CAST(s.id AS INTEGER) ASC LIMIT ?",
            (int(since_id), int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            target = r["target"] or ""
            if target.startswith("b-agent:"):
                task_type = target[len("b-agent:"):]
            elif target.startswith("c-agent:"):
                task_type = target[len("c-agent:"):]
            else:
                task_type = (r["task_type"] or target or "").strip()
            try:
                rating = int(r["quality_rating"])
            except (TypeError, ValueError):
                continue
            latency = r["b_latency_ms"]
            if latency is None:
                latency = self._dispatch_latency_ms(
                    r["claimed_at"], r["completed_at"])
            out.append({
                "task_type": task_type,
                "model": r["actual_model_used"],
                "rating": rating,
                "dispatch_id": str(r["id"]),
                "cost_usd": (float(r["result_cost_usd"])
                             if r["result_cost_usd"] is not None else None),
                "latency_ms": latency,
            })
        return out

    # ── Slice 10 (2026-05-20): Category B agent helpers ─────────────
    # B agents are stateless callables (Sonnet calls with frozen
    # system prompts + snapshot-on-demand inputs) dispatched as tools
    # by the overseer. They share sibling_tasks for the audit row
    # (status='completed' immediately because B runs synchronously)
    # and write their snapshot + full output into b_invocation_-
    # transcripts. The daily B cap is separate from the A sibling cap.

    def b_agent_dispatch(self, *, b_agent_name, prompt, snapshot,
                          output_text, model_used, cost_usd, latency_ms,
                          retention_days=30, daily_cap=50,
                          marker_required=True) -> dict:
        """Persist a completed B-agent invocation.

        Creates a sibling_tasks row (target='b-agent:<name>',
        status='completed') so the existing rate/audit/freshness
        plumbing works for B tasks without forking it, and writes the
        snapshot + full output into b_invocation_transcripts with a
        retention horizon.

        Daily cap is enforced per-B-agent — separate from A's cap
        because B is cheaper and we want to allow more of them.

        Returns {ok, sibling_task_id, transcript_id, used_today, cap}
        or {ok: False, error} on cap exhaustion / validation failure.
        """
        import json as _json
        # Cap check first — counts ALL B dispatches today across all
        # B agents (cheap protection against runaway loops). Per-B
        # tuning can come later if we observe one B starving others.
        day_start_iso = self._local_day_start_iso()
        used_today = self._conn.execute(
            "SELECT COUNT(*) AS n FROM sibling_tasks "
            "WHERE created_by = 'overseer' "
            "AND target LIKE 'b-agent:%' "
            "AND created_at >= ?",
            (day_start_iso,),
        ).fetchone()["n"]
        if used_today >= daily_cap:
            return {
                "ok": False,
                "error": (f"daily B-agent dispatch cap reached "
                          f"({used_today}/{daily_cap})"),
                "cap": daily_cap,
                "used_today": used_today,
            }

        # Validate marker if required. The B's job is to prepend a
        # [B:<short-name>] marker so downstream consolidation can
        # spot B authorship. Defensive: if the model dropped it,
        # we caller-decide whether to wrap or error.
        if marker_required:
            expected_marker = f"[B:{b_agent_name.replace('_', '-')}]"
            if expected_marker not in output_text:
                return {
                    "ok": False,
                    "error": (f"output missing required marker "
                              f"'{expected_marker}'"),
                    "expected_marker": expected_marker,
                }

        target = f"b-agent:{b_agent_name}"
        ctx_json = _json.dumps(
            {"snapshot_summary":
                f"<see b_invocation_transcripts for full snapshot>",
             "b_agent_name": b_agent_name},
            default=str, ensure_ascii=False,
        )
        cur = self._conn.execute(
            "INSERT INTO sibling_tasks "
            "  (created_by, target, prompt, context_json, "
            "   cost_budget_usd, task_type, preferred_model_tier, "
            "   status, claimed_at, claimed_by, completed_at, "
            "   result_text, actual_model_used, result_cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "        datetime('now'), ?, datetime('now'), ?, ?, ?)",
            ("overseer", target, prompt, ctx_json,
             float(cost_usd) + 0.01,  # nominal budget for audit
             "audit",  # B's task_type is always 'audit' for now
             "balanced", "completed",
             f"b-agent:{b_agent_name}",  # claimed_by self-reference
             output_text, model_used, float(cost_usd)),
        )
        sibling_task_id = cur.lastrowid

        # Compute retention timestamp
        from datetime import datetime, timezone, timedelta
        retained_until = (
            datetime.now(timezone.utc) + timedelta(days=retention_days)
        ).strftime("%Y-%m-%d %H:%M:%S")

        snap_json = _json.dumps(snapshot, default=str, ensure_ascii=False)
        tcur = self._conn.execute(
            "INSERT INTO b_invocation_transcripts "
            "  (sibling_task_id, b_agent_name, snapshot_json, "
            "   output_text, model_used, cost_usd, latency_ms, "
            "   retained_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sibling_task_id, b_agent_name, snap_json,
             output_text, model_used,
             float(cost_usd), int(latency_ms), retained_until),
        )
        transcript_id = tcur.lastrowid
        self._safe_commit()
        return {
            "ok": True,
            "sibling_task_id": sibling_task_id,
            "transcript_id": transcript_id,
            "used_today": used_today + 1,
            "cap": daily_cap,
            "retained_until": retained_until,
        }

    def b_agent_recent(self, *, b_agent_name=None, limit=20) -> list:
        """Recent B invocations (joined with sibling_tasks for ratings).
        Used by the C-graduation detector (Slice 10.3) and by the
        overseer's chat tool that lets it review its own audit history.
        """
        sql = (
            "SELECT t.id AS transcript_id, t.b_agent_name, "
            "       t.snapshot_json, t.output_text, t.model_used, "
            "       t.cost_usd, t.latency_ms, t.created_at, "
            "       t.retained_until, "
            "       s.id AS sibling_task_id, "
            "       s.quality_rating, s.quality_notes, "
            "       s.dataset_candidate "
            "FROM b_invocation_transcripts t "
            "JOIN sibling_tasks s ON s.id = t.sibling_task_id "
            "WHERE 1=1 "
        )
        params: list = []
        if b_agent_name:
            sql += "AND t.b_agent_name = ? "
            params.append(b_agent_name)
        sql += "ORDER BY t.created_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def b_agent_gc_expired(self) -> int:
        """Daily GC step: delete transcripts past their retention.
        Returns deleted row count. The sibling_tasks rows stay so the
        audit ledger is intact — only the (sometimes large) snapshot
        JSON drops out."""
        cur = self._conn.execute(
            "DELETE FROM b_invocation_transcripts "
            "WHERE retained_until < datetime('now')"
        )
        n = cur.rowcount
        self._safe_commit()
        if n:
            log.info("b_agent_gc_expired: deleted %d expired transcripts", n)
        return int(n or 0)

    def b_agent_stats(self, *, window_days=7) -> dict:
        """Per-B-agent dispatch + rating stats over a rolling window.
        Used by C-graduation detector (≥10 dispatches AND ≥7 rated 4+
        in past 7 days → propose graduation to Tory)."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=window_days)).strftime(
            "%Y-%m-%d %H:%M:%S")
        rows = self._conn.execute(
            "SELECT t.b_agent_name AS name, "
            "       COUNT(*) AS dispatches, "
            "       SUM(CASE WHEN s.quality_rating >= 4 THEN 1 ELSE 0 END) "
            "         AS rated_4_plus, "
            "       SUM(CASE WHEN s.quality_rating IS NULL THEN 1 ELSE 0 END) "
            "         AS unrated, "
            "       MAX(t.created_at) AS last_dispatch_at "
            "FROM b_invocation_transcripts t "
            "JOIN sibling_tasks s ON s.id = t.sibling_task_id "
            "WHERE t.created_at >= ? "
            "GROUP BY t.b_agent_name "
            "ORDER BY dispatches DESC",
            (cutoff,),
        ).fetchall()
        return {
            "window_days": window_days,
            "by_agent": [dict(r) for r in rows],
        }

    # ── Slice 10 CP5 (2026-05-20): C-agent helpers ─────────────────

    # Graduation thresholds. Kept as class constants so the
    # graduation detector + the docs/notifications can read the
    # same numbers. Per locked design (agent_ecosystem_design.md):
    # ≥10 dispatches AND ≥7 rated 4+ in a rolling 7-day window.
    C_GRADUATION_MIN_DISPATCHES = 10
    C_GRADUATION_MIN_RATED_4PLUS = 7
    C_GRADUATION_WINDOW_DAYS = 7

    def list_c_agents(self, *, status=None, limit=50) -> list:
        """List C agents. Optional status filter."""
        sql = "SELECT * FROM c_agents"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_c_agent_by_name(self, name) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM c_agents WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def promote_b_to_c(self, *, b_agent_name, c_agent_name,
                        system_prompt, model,
                        cadence_minutes=1440,
                        dispatches_at_promotion=0,
                        rated_4plus_at_promotion=0) -> dict:
        """Create a c_agents row promoting a B pattern to a C agent.

        The B parent's system_prompt is frozen at promotion time —
        future B changes don't propagate to C. C may diverge over
        time. Returns {ok, c_agent_id} or {ok: False, error}.
        """
        existing = self.get_c_agent_by_name(c_agent_name)
        if existing:
            return {
                "ok": False,
                "error": f"c_agent '{c_agent_name}' already exists "
                         f"(id {existing['id']})",
            }
        cur = self._conn.execute(
            "INSERT INTO c_agents (name, graduated_from_b_name, "
            "  cadence_minutes, system_prompt, model, "
            "  graduated_from_b_dispatches_count, "
            "  graduated_from_b_rated_4plus_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (c_agent_name, b_agent_name, int(cadence_minutes),
             system_prompt, model,
             int(dispatches_at_promotion),
             int(rated_4plus_at_promotion)),
        )
        self._safe_commit()
        log.info("promote_b_to_c: %s -> c_agent_id %d", c_agent_name,
                 cur.lastrowid)
        return {"ok": True, "c_agent_id": cur.lastrowid,
                "name": c_agent_name}

    def update_c_agent_run(self, *, c_agent_id, sibling_task_id) -> None:
        """Update last_run_at + last_run_sibling_task_id after a
        scheduled C run."""
        self._conn.execute(
            "UPDATE c_agents SET last_run_at = datetime('now'), "
            "  last_run_sibling_task_id = ? "
            "WHERE id = ?",
            (int(sibling_task_id), int(c_agent_id)),
        )
        self._safe_commit()

    def list_due_c_agents(self) -> list:
        """List active C agents whose cadence_minutes has elapsed
        since last_run_at (or that have never run). Used by the
        _run_scheduled_c_agents tick step."""
        rows = self._conn.execute(
            "SELECT * FROM c_agents "
            "WHERE status = 'active' "
            "AND (last_run_at IS NULL "
            "     OR julianday('now') - julianday(last_run_at) "
            "        >= cadence_minutes / 1440.0) "
            "ORDER BY last_run_at ASC NULLS FIRST"
        ).fetchall()
        return [dict(r) for r in rows]

    def check_c_graduations(self, *, min_dispatches=None,
                              min_rated_4plus=None,
                              window_days=None) -> list:
        """Return list of B agents that meet the C graduation
        thresholds AND don't already have a C row. Caller is
        responsible for emitting a notification with custom actions.

        Thresholds default to the class constants
        (C_GRADUATION_MIN_DISPATCHES, _MIN_RATED_4PLUS, _WINDOW_DAYS)
        but can be overridden via kwargs so plugin.toml can ship
        looser values during shake-out testing. Class constants
        remain the locked-design reference values.

        Returns: [{"b_agent_name": ..., "dispatches": ...,
                   "rated_4_plus": ..., "proposed_c_name": ...}]
        """
        if min_dispatches is None:
            min_dispatches = self.C_GRADUATION_MIN_DISPATCHES
        if min_rated_4plus is None:
            min_rated_4plus = self.C_GRADUATION_MIN_RATED_4PLUS
        if window_days is None:
            window_days = self.C_GRADUATION_WINDOW_DAYS
        stats = self.b_agent_stats(window_days=int(window_days))
        proposals = []
        for s in stats.get("by_agent") or []:
            if (s["dispatches"] >= int(min_dispatches)
                    and s["rated_4_plus"] >= int(min_rated_4plus)):
                b_name = s["name"]
                # Don't propose if a C already exists for this B
                existing = self._conn.execute(
                    "SELECT id FROM c_agents "
                    "WHERE graduated_from_b_name = ?",
                    (b_name,),
                ).fetchone()
                if existing:
                    continue
                proposals.append({
                    "b_agent_name": b_name,
                    "dispatches": s["dispatches"],
                    "rated_4_plus": s["rated_4_plus"],
                    "proposed_c_name": b_name.replace("_", "-") + "-daily",
                })
        return proposals

    # ── Slice 10.4 Phase 2 (2026-05-20): unified runs view ──────────
    # The Activity tab needs a single timeline of "what overseer did"
    # across 5 source tables: b_invocation_transcripts (B+C agent
    # runs), sibling_tasks (A-tier sibling dispatches that aren't
    # B/C — filtered by NOT LIKE 'b-agent:%' AND NOT LIKE 'c-agent:%'),
    # chat_messages (assistant turns), and overseer_journal (tick
    # reflections). Each row is normalized to a common shape so the
    # frontend renders them in one timeline.

    def list_recent_runs(self, *, hours=24, limit=200,
                          kinds=None) -> list:
        """Return last N runs across all overseer surfaces, newest
        first. Each run is a dict with: id, kind, started_at,
        ended_at, summary, cost_usd, latency_ms, tool_calls_count,
        rateable (bool), sibling_task_id (or None), current_rating
        (or None), model (or '').

        kinds: optional set of {'b_agent','c_agent','sibling',
        'chat_turn','journal_step'} to filter the union. None = all.
        """
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=int(hours))).strftime(
            "%Y-%m-%d %H:%M:%S")
        wanted = set(kinds) if kinds else {
            "b_agent", "c_agent", "sibling",
            "chat_turn", "journal_step",
        }
        runs: list = []

        # ── B + C agent runs (from b_invocation_transcripts) ──
        if "b_agent" in wanted or "c_agent" in wanted:
            rows = self._conn.execute(
                "SELECT t.id AS trans_id, t.b_agent_name, "
                "       t.created_at, t.cost_usd, t.latency_ms, "
                "       t.model_used, substr(t.output_text, 1, 200) "
                "         AS output_excerpt, "
                "       s.id AS sibling_task_id, s.target, "
                "       s.quality_rating "
                "FROM b_invocation_transcripts t "
                "LEFT JOIN sibling_tasks s ON s.id = t.sibling_task_id "
                "WHERE t.created_at >= ? "
                "ORDER BY t.created_at DESC",
                (cutoff,),
            ).fetchall()
            for r in rows:
                target = r["target"] or ""
                kind = ("c_agent" if target.startswith("c-agent:")
                        else "b_agent")
                if kind not in wanted:
                    continue
                runs.append({
                    "id": f"b-trans:{r['trans_id']}",
                    "kind": kind,
                    "subkind": r["b_agent_name"],
                    "started_at": r["created_at"],
                    "ended_at": r["created_at"],
                    "summary": (r["output_excerpt"] or "").strip(),
                    "cost_usd": float(r["cost_usd"] or 0),
                    "latency_ms": int(r["latency_ms"] or 0),
                    "tool_calls_count": 0,  # B is one LLM call, no tools
                    "model": r["model_used"] or "",
                    "rateable": r["sibling_task_id"] is not None,
                    "sibling_task_id": r["sibling_task_id"],
                    "current_rating": r["quality_rating"],
                })

        # ── A-tier sibling dispatches (sibling_tasks NOT b-agent/c-agent) ─
        if "sibling" in wanted:
            rows = self._conn.execute(
                "SELECT id, created_at, completed_at, target, prompt, "
                "       cost_budget_usd, result_cost_usd, status, "
                "       actual_model_used, quality_rating, claimed_at "
                "FROM sibling_tasks "
                "WHERE target NOT LIKE 'b-agent:%' "
                "AND target NOT LIKE 'c-agent:%' "
                "AND created_at >= ? "
                "ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
            for r in rows:
                runs.append({
                    "id": f"sibling:{r['id']}",
                    "kind": "sibling",
                    "subkind": r["target"] or "any",
                    "started_at": r["created_at"],
                    "ended_at": (r["completed_at"] or r["claimed_at"]
                                 or r["created_at"]),
                    "summary": (r["prompt"] or "")[:200].strip(),
                    "cost_usd": float(r["result_cost_usd"] or 0),
                    "latency_ms": 0,  # sibling runs are async, no in-band ms
                    "tool_calls_count": 0,
                    "model": r["actual_model_used"] or "",
                    "rateable": (r["status"] == "completed"
                                 and r["quality_rating"] is None),
                    "sibling_task_id": r["id"],
                    "current_rating": r["quality_rating"],
                    "status": r["status"],
                })

        # ── Chat turns (assistant role only — user is the trigger) ──
        if "chat_turn" in wanted:
            import json as _json
            rows = self._conn.execute(
                "SELECT id, created_at, model, latency_ms, cost_usd, "
                "       substr(content, 1, 200) AS content_excerpt, "
                "       metadata_json "
                "FROM chat_messages "
                "WHERE role = 'assistant' "
                "AND created_at >= ? "
                "ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
            for r in rows:
                tool_calls_count = 0
                try:
                    meta = _json.loads(r["metadata_json"] or "{}")
                    tool_calls_count = len(meta.get("tool_calls") or [])
                except Exception:
                    pass
                runs.append({
                    "id": f"chat:{r['id']}",
                    "kind": "chat_turn",
                    "subkind": "assistant",
                    "started_at": r["created_at"],
                    "ended_at": r["created_at"],
                    "summary": (r["content_excerpt"] or "").strip(),
                    "cost_usd": float(r["cost_usd"] or 0),
                    "latency_ms": int(r["latency_ms"] or 0),
                    "tool_calls_count": tool_calls_count,
                    "model": r["model"] or "",
                    "rateable": False,
                    "sibling_task_id": None,
                    "current_rating": None,
                })

        # ── Journal entries (tool-enabled tick reflections) ──
        if "journal_step" in wanted:
            import json as _json
            rows = self._conn.execute(
                "SELECT id, written_at, instance_id, triggered_by, "
                "       substr(body, 1, 200) AS body_excerpt, "
                "       provisionality, referenced_artifacts, "
                "       backend, model, cost_usd, latency_ms "
                "FROM overseer_journal "
                "WHERE written_at >= ? "
                "ORDER BY written_at DESC",
                (cutoff,),
            ).fetchall()
            for r in rows:
                tool_calls_count = 0
                try:
                    ra = _json.loads(r["referenced_artifacts"] or "[]")
                    for art in ra:
                        if (isinstance(art, dict)
                                and art.get("type") == "tool_calls"):
                            tool_calls_count = len(art.get("calls") or [])
                            break
                except Exception:
                    pass
                runs.append({
                    "id": f"journal:{r['id']}",
                    "kind": "journal_step",
                    "subkind": r["triggered_by"] or "scheduled",
                    "started_at": r["written_at"],
                    "ended_at": r["written_at"],
                    "summary": (r["body_excerpt"] or "").strip(),
                    "cost_usd": float(r["cost_usd"] or 0),
                    "latency_ms": int(r["latency_ms"] or 0),
                    "tool_calls_count": tool_calls_count,
                    "model": r["model"] or "",
                    "rateable": False,
                    "sibling_task_id": None,
                    "current_rating": None,
                    "provisionality": r["provisionality"],
                })

        # Sort all runs by started_at DESC, cap at limit
        runs.sort(key=lambda x: x["started_at"] or "", reverse=True)
        return runs[:int(limit)]

    def get_run_detail(self, *, kind, run_id):
        """Return a single run's full detail for the trace viewer.

        Returns a dict with: trigger, nodes, edges, full_prompt,
        full_output, raw, plus all the list_recent_runs fields.

        kind ∈ {'b_agent','c_agent','sibling','chat_turn','journal_step'}
        run_id is the numeric id (after the colon in the unified id).
        """
        import json as _json
        if kind in ("b_agent", "c_agent"):
            return self._run_detail_b_agent(int(run_id))
        if kind == "sibling":
            return self._run_detail_sibling(int(run_id))
        if kind == "chat_turn":
            return self._run_detail_chat(int(run_id))
        if kind == "journal_step":
            return self._run_detail_journal(int(run_id))
        return {"ok": False, "error": f"unknown run kind: {kind}"}

    def _run_detail_b_agent(self, trans_id):
        import json as _json
        row = self._conn.execute(
            "SELECT t.*, s.target, s.prompt AS sibling_prompt, "
            "       s.quality_rating, s.quality_notes "
            "FROM b_invocation_transcripts t "
            "LEFT JOIN sibling_tasks s ON s.id = t.sibling_task_id "
            "WHERE t.id = ?",
            (trans_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "transcript not found"}
        row = dict(row)
        try:
            snapshot = _json.loads(row.get("snapshot_json") or "{}")
        except Exception:
            snapshot = {"_parse_error": True}
        target = row.get("target") or ""
        kind = "c_agent" if target.startswith("c-agent:") else "b_agent"
        # Build flow graph: trigger -> snapshot -> LLM -> output
        nodes = [
            {"id": "trigger", "kind": "trigger",
             "label": f"Tool: dispatch_{kind}_{row['b_agent_name']}",
             "sublabel": f"target={target}"},
            {"id": "snapshot", "kind": "snapshot",
             "label": "Snapshot built",
             "sublabel": f"{len(row.get('snapshot_json') or '')} chars"},
            {"id": "llm", "kind": "llm_call",
             "label": row.get("model_used") or "(model)",
             "sublabel": (f"${row.get('cost_usd') or 0:.4f} · "
                          f"{row.get('latency_ms') or 0}ms")},
            {"id": "output", "kind": "output",
             "label": "Output",
             "sublabel": (row.get("output_text") or "")[:80]},
        ]
        edges = [
            {"source": "trigger", "target": "snapshot"},
            {"source": "snapshot", "target": "llm"},
            {"source": "llm", "target": "output"},
        ]
        return {
            "ok": True,
            "id": f"b-trans:{trans_id}",
            "kind": kind,
            "subkind": row.get("b_agent_name"),
            "started_at": row.get("created_at"),
            "ended_at": row.get("created_at"),
            "cost_usd": float(row.get("cost_usd") or 0),
            "latency_ms": int(row.get("latency_ms") or 0),
            "model": row.get("model_used") or "",
            "sibling_task_id": row.get("sibling_task_id"),
            "current_rating": row.get("quality_rating"),
            "current_notes": row.get("quality_notes"),
            "rateable": row.get("sibling_task_id") is not None,
            "nodes": nodes,
            "edges": edges,
            "full_prompt": (
                f"=== Snapshot ===\n"
                f"{_json.dumps(snapshot, indent=2, default=str)}"
            ),
            "full_output": row.get("output_text") or "",
            "raw": row,
        }

    def _run_detail_sibling(self, sid):
        row = self._conn.execute(
            "SELECT * FROM sibling_tasks WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "sibling task not found"}
        row = dict(row)
        # Build flow graph: dispatch -> claim -> complete
        nodes = [
            {"id": "dispatch", "kind": "trigger",
             "label": f"Dispatched by {row.get('created_by') or '?'}",
             "sublabel": f"target={row.get('target') or '?'}"},
        ]
        edges = []
        if row.get("claimed_at"):
            nodes.append({
                "id": "claim", "kind": "step",
                "label": f"Claimed by {row.get('claimed_by') or '?'}",
                "sublabel": row.get("claimed_at"),
            })
            edges.append({"source": "dispatch", "target": "claim"})
        if row.get("completed_at"):
            nodes.append({
                "id": "complete", "kind": "output",
                "label": f"Status: {row.get('status') or '?'}",
                "sublabel": (f"${row.get('result_cost_usd') or 0:.4f} · "
                             f"{row.get('actual_model_used') or '?'}"),
            })
            edges.append({
                "source": "claim" if row.get("claimed_at") else "dispatch",
                "target": "complete",
            })
        return {
            "ok": True,
            "id": f"sibling:{sid}",
            "kind": "sibling",
            "subkind": row.get("target") or "any",
            "started_at": row.get("created_at"),
            "ended_at": row.get("completed_at") or row.get("created_at"),
            "cost_usd": float(row.get("result_cost_usd") or 0),
            "latency_ms": 0,
            "model": row.get("actual_model_used") or "",
            "sibling_task_id": sid,
            "current_rating": row.get("quality_rating"),
            "current_notes": row.get("quality_notes"),
            "rateable": (row.get("status") == "completed"),
            "status": row.get("status"),
            "nodes": nodes,
            "edges": edges,
            "full_prompt": row.get("prompt") or "",
            "full_output": row.get("result_text") or "",
            "raw": row,
        }

    def _run_detail_chat(self, msg_id):
        import json as _json
        # Get this assistant message + the preceding user message
        row = self._conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "chat message not found"}
        row = dict(row)
        # Agent harness: scope to the row's own thread and use id
        # ordering — a created_at-only scan can pick another thread's
        # (or a same-second later) user message as the trigger.
        user_row = self._conn.execute(
            "SELECT id, content, created_at FROM chat_messages "
            "WHERE role = 'user' AND thread_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT 1",
            (row.get("thread_id") or 0, int(msg_id)),
        ).fetchone()
        meta = {}
        try:
            meta = _json.loads(row.get("metadata_json") or "{}")
        except Exception:
            pass
        tool_calls = meta.get("tool_calls") or []
        # Build flow graph
        nodes = [
            {"id": "user", "kind": "trigger",
             "label": "User message",
             "sublabel": ((user_row["content"] or "")[:60]
                          if user_row else "(no preceding user msg)")},
            {"id": "llm0", "kind": "llm_call",
             "label": row.get("model") or "(model)",
             "sublabel": (f"${row.get('cost_usd') or 0:.4f} · "
                          f"{row.get('latency_ms') or 0}ms")},
        ]
        edges = [{"source": "user", "target": "llm0"}]
        # Tool calls fan out from the LLM
        for i, tc in enumerate(tool_calls):
            nid = f"tc{i}"
            nodes.append({
                "id": nid, "kind": "tool_call",
                "label": tc.get("name") or "?",
                "sublabel": (f"iter={tc.get('iter', 0)} · "
                             f"{tc.get('result_chars', 0)} chars"),
            })
            edges.append({"source": "llm0", "target": nid})
        nodes.append({
            "id": "reply", "kind": "output",
            "label": "Reply",
            "sublabel": (row.get("content") or "")[:80],
        })
        # Reply edge comes from last tool call, else from LLM directly
        if tool_calls:
            edges.append({"source": f"tc{len(tool_calls)-1}", "target": "reply"})
        else:
            edges.append({"source": "llm0", "target": "reply"})
        return {
            "ok": True,
            "id": f"chat:{msg_id}",
            "kind": "chat_turn",
            "subkind": "assistant",
            "started_at": row.get("created_at"),
            "ended_at": row.get("created_at"),
            "cost_usd": float(row.get("cost_usd") or 0),
            "latency_ms": int(row.get("latency_ms") or 0),
            "model": row.get("model") or "",
            "rateable": False,
            "nodes": nodes,
            "edges": edges,
            "full_prompt": (user_row["content"] if user_row else ""),
            "full_output": row.get("content") or "",
            "tool_calls": tool_calls,
            "raw": {
                "message": row,
                "user_message": dict(user_row) if user_row else None,
                "metadata": meta,
            },
        }

    def _run_detail_journal(self, jid):
        import json as _json
        row = self._conn.execute(
            "SELECT * FROM overseer_journal WHERE id = ?", (jid,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "journal entry not found"}
        row = dict(row)
        try:
            ra = _json.loads(row.get("referenced_artifacts") or "[]")
        except Exception:
            ra = []
        tool_calls = []
        for art in ra:
            if (isinstance(art, dict)
                    and art.get("type") == "tool_calls"):
                tool_calls = art.get("calls") or []
                break
        try:
            tick_summary = _json.loads(
                row.get("tick_summary_json") or "{}")
        except Exception:
            tick_summary = {}
        # Build flow graph
        nodes = [
            {"id": "tick", "kind": "trigger",
             "label": f"Tick: {row.get('triggered_by') or 'scheduled'}",
             "sublabel": row.get("instance_id") or ""},
            {"id": "llm0", "kind": "llm_call",
             "label": row.get("model") or "(model)",
             "sublabel": (f"${row.get('cost_usd') or 0:.4f} · "
                          f"{row.get('latency_ms') or 0}ms")},
        ]
        edges = [{"source": "tick", "target": "llm0"}]
        for i, tc in enumerate(tool_calls):
            nid = f"tc{i}"
            nodes.append({
                "id": nid, "kind": "tool_call",
                "label": tc.get("name") or "?",
                "sublabel": (f"iter={tc.get('iter', 0)} · "
                             f"{tc.get('result_chars', 0)} chars"
                             + (" · BLOCKED"
                                if tc.get("blocked") else "")),
            })
            edges.append({"source": "llm0", "target": nid})
        nodes.append({
            "id": "entry", "kind": "output",
            "label": f"Entry [prov:{row.get('provisionality') or '?'}]",
            "sublabel": (row.get("body") or "")[:80],
        })
        if tool_calls:
            edges.append({
                "source": f"tc{len(tool_calls)-1}", "target": "entry",
            })
        else:
            edges.append({"source": "llm0", "target": "entry"})
        return {
            "ok": True,
            "id": f"journal:{jid}",
            "kind": "journal_step",
            "subkind": row.get("triggered_by") or "scheduled",
            "started_at": row.get("written_at"),
            "ended_at": row.get("written_at"),
            "cost_usd": float(row.get("cost_usd") or 0),
            "latency_ms": int(row.get("latency_ms") or 0),
            "model": row.get("model") or "",
            "provisionality": row.get("provisionality"),
            "rateable": False,
            "nodes": nodes,
            "edges": edges,
            "full_prompt": "(journal prompt — see prompts source)",
            "full_output": row.get("body") or "",
            "tool_calls": tool_calls,
            "tick_summary": tick_summary,
            "raw": row,
        }

    def export_runs_bundle(self, *, hours=24) -> dict:
        """Build a JSON bundle of all runs in the past N hours with
        FULL detail (prompts, outputs, snapshots). Used by the
        Activity tab's "Export 24h" button to produce a debug
        bundle that can be attached to bug reports or read offline.
        """
        from datetime import datetime, timezone
        runs = self.list_recent_runs(hours=hours, limit=1000)
        details = []
        for r in runs:
            kind = r["kind"]
            # Extract numeric id from unified "kind:N" form
            raw_id = r["id"].split(":", 1)[1] if ":" in r["id"] else r["id"]
            try:
                detail = self.get_run_detail(kind=kind, run_id=raw_id)
                if detail.get("ok"):
                    details.append(detail)
            except Exception as e:
                details.append({
                    "ok": False, "id": r["id"], "error": str(e)[:200],
                })
        return {
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "window_hours": int(hours),
            "run_count": len(details),
            "runs": details,
        }

    # ── Slice 13 (2026-05-21): sensitivity tier helpers ─────────────

    # Strictness order — a rule can only promote toward the right.
    SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")
    # Default retention per tier when a rule doesn't specify.
    _TIER_DEFAULT_RETENTION = {
        "public": "keep-raw",
        "internal": "keep-raw",
        "confidential": "gist-and-drop",
        "restricted": "no-import",
    }

    def _tier_rank(self, tier) -> int:
        try:
            return self.SENSITIVITY_ORDER.index(tier or "public")
        except ValueError:
            return 0

    def get_sensitivity_rules(self, *, active_only=True) -> list:
        sql = "SELECT * FROM sensitivity_rules"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY priority DESC, id ASC"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def resolve_sensitivity(self, *, cwd="", source="",
                             project="") -> dict:
        """Resolve a session's sensitivity from the active rules.

        Returns {tier, retention_policy, matched_rule_id, set_by}.
        The STRICTEST matching tier wins (a rule can only promote);
        priority breaks ties among same-tier matches. No match →
        public / keep-raw.
        """
        rules = self.get_sensitivity_rules(active_only=True)
        best = {
            "tier": "public",
            "retention_policy": "keep-raw",
            "matched_rule_id": None,
            "set_by": "default",
        }
        cwd_l = (cwd or "").lower()
        proj_l = (project or "").lower()
        for r in rules:
            mt = r["match_type"]
            pat = (r["pattern"] or "")
            pat_l = pat.lower()
            hit = False
            if mt == "cwd_like":
                hit = self._like_match(cwd_l, pat_l)
            elif mt == "project_like":
                hit = self._like_match(proj_l, pat_l)
            elif mt == "source":
                hit = (source == pat)
            if not hit:
                continue
            # Promote only — keep the strictest tier seen so far.
            if self._tier_rank(r["tier"]) > self._tier_rank(best["tier"]):
                best = {
                    "tier": r["tier"],
                    "retention_policy": (
                        r["retention_policy"]
                        or self._TIER_DEFAULT_RETENTION.get(
                            r["tier"], "keep-raw")),
                    "matched_rule_id": r["id"],
                    "set_by": "rule",
                }
        return best

    @staticmethod
    def _like_match(value: str, pattern: str) -> bool:
        """Minimal SQL-LIKE matcher (% = any run, no _ support needed
        for our patterns). Patterns are already lowercased by caller."""
        if "%" not in pattern:
            return pattern in value
        parts = pattern.split("%")
        pos = 0
        # Leading non-% must be a prefix.
        if parts[0] and not value.startswith(parts[0]):
            return False
        # Trailing non-% must be a suffix.
        if parts[-1] and not value.endswith(parts[-1]):
            return False
        for part in parts:
            if not part:
                continue
            idx = value.find(part, pos)
            if idx < 0:
                return False
            pos = idx + len(part)
        return True

    def set_session_sensitivity(self, session_id, *, tier,
                                 retention_policy, set_by,
                                 force_demote=False) -> bool:
        """Write the resolved sensitivity onto an imported_sessions
        row. Promote-only unless force_demote (user override). Returns
        True if a write happened."""
        row = self._conn.execute(
            "SELECT sensitivity FROM imported_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return False
        current = row["sensitivity"]
        if (current and not force_demote
                and self._tier_rank(tier) <= self._tier_rank(current)):
            return False  # would demote / no-op; skip
        self._conn.execute(
            "UPDATE imported_sessions SET sensitivity = ?, "
            "  retention_policy = ?, sensitivity_set_by = ?, "
            "  sensitivity_set_at = datetime('now') WHERE id = ?",
            (tier, retention_policy, set_by, session_id),
        )
        self._safe_commit()
        return True

    def backfill_sensitivity(self, *, only_unset=True) -> dict:
        """Apply the active rules to existing imported_sessions.
        only_unset=True skips rows that already have a sensitivity
        (so a user override or scanner promotion isn't clobbered).
        Returns per-tier counts."""
        sql = ("SELECT id, cwd, source, project FROM imported_sessions")
        if only_unset:
            sql += " WHERE sensitivity IS NULL"
        rows = self._conn.execute(sql).fetchall()
        counts = {}
        for r in rows:
            res = self.resolve_sensitivity(
                cwd=r["cwd"], source=r["source"], project=r["project"])
            tier = res["tier"]
            # Even 'public' gets written so set_by reflects the pass.
            self._conn.execute(
                "UPDATE imported_sessions SET sensitivity = ?, "
                "  retention_policy = ?, sensitivity_set_by = ?, "
                "  sensitivity_set_at = datetime('now') WHERE id = ?",
                (tier, res["retention_policy"],
                 res["set_by"], r["id"]),
            )
            counts[tier] = counts.get(tier, 0) + 1
        self._safe_commit()
        return {"scanned": len(rows), "by_tier": counts}

    # ── Slice 14.7.3 (2026-05-26): work/personal/cortex category ──

    # Rule order: first match wins. Confidential sensitivity short-
    # circuits to work (ProjectX/ClientB/clinical). Then cwd patterns, then
    # web-AI fallthrough to unclassified (for LLM classifier later).
    _CATEGORY_RULES = [
        # (kind, pattern, category)
        # kind ∈ {'sensitivity', 'cwd_lower_contains', 'project_lower_contains', 'source_eq'}
        ("sensitivity",         "confidential",      "work"),
        ("sensitivity",         "restricted",        "work"),
        # Generic fallback ONLY. Real instance rules -- the employer/client
        # cwd patterns AND the owner's own project names (cortex/personal) --
        # load from the gitignored local config via config_loader.category_rules()
        # (see _effective_category_rules). With no local config, only
        # sensitivity-tagged work is categorized; everything else is
        # 'unclassified' and falls through to the LLM classifier.
    ]

    def _effective_category_rules(self):
        """The gitignored local config's category rules if present (real
        instance + project patterns), else the generic in-code fallback."""
        try:
            from . import config_loader as _cl
        except Exception:
            try:
                import config_loader as _cl
            except Exception:
                _cl = None
        if _cl is not None:
            rules = _cl.category_rules()
            if rules:
                return [(r.get("kind"), r.get("pattern"), r.get("category"))
                        for r in rules]
        return self._CATEGORY_RULES

    def resolve_category(self, *, cwd="", source="", project="",
                          sensitivity="") -> dict:
        """Rule-based classifier — Slice 14.7.3.

        Returns {category, set_by, matched_rule}. category is one of:
          'work' | 'cortex' | 'personal' | 'unclassified'

        'unclassified' is the default for sessions where no rule
        matches — typically web-AI conversations (chatgpt, grok-com,
        grok-twitter) that have no cwd. Those get a follow-up pass
        by the Flash LLM classifier.
        """
        cwd_l = (cwd or "").lower()
        proj_l = (project or "").lower()
        sens = (sensitivity or "").lower()
        for kind, pattern, cat in self._effective_category_rules():
            hit = False
            if kind == "sensitivity":
                hit = (sens == pattern)
            elif kind == "cwd_lower_contains":
                hit = bool(cwd_l) and (pattern in cwd_l)
            elif kind == "project_lower_contains":
                hit = bool(proj_l) and (pattern in proj_l)
            elif kind == "source_eq":
                hit = (source == pattern)
            if hit:
                return {
                    "category": cat,
                    "set_by": "rule",
                    "matched_rule": f"{kind}:{pattern}",
                }
        return {
            "category": "unclassified",
            "set_by": "rule-no-match",
            "matched_rule": None,
        }

    def backfill_categories(self, *, only_unset=True) -> dict:
        """Apply rule-based classifier to existing imported_sessions.
        only_unset=True skips rows that already carry a non-empty
        category (so LLM-classifier results and manual overrides
        aren't clobbered).

        Returns {scanned, by_category, by_set_by}.
        """
        sql = ("SELECT id, cwd, source, project, sensitivity "
               "FROM imported_sessions")
        if only_unset:
            sql += " WHERE COALESCE(category,'') = ''"
        rows = self._conn.execute(sql).fetchall()
        cat_counts: dict = {}
        for r in rows:
            res = self.resolve_category(
                cwd=r["cwd"], source=r["source"],
                project=r["project"], sensitivity=r["sensitivity"])
            self._conn.execute(
                "UPDATE imported_sessions SET category = ?, "
                "  category_set_by = ?, "
                "  category_set_at = datetime('now') WHERE id = ?",
                (res["category"], res["set_by"], r["id"]),
            )
            cat_counts[res["category"]] = (
                cat_counts.get(res["category"], 0) + 1)
        self._safe_commit()
        return {"scanned": len(rows), "by_category": cat_counts}

    def set_session_category(self, imported_id: str, *, category: str,
                              set_by: str = "manual") -> bool:
        """Explicit category set — used by LLM classifier batch +
        manual overrides. Allowed categories enforced."""
        if category not in ("work", "cortex", "personal",
                             "unclassified"):
            raise ValueError(f"invalid category: {category}")
        cur = self._conn.execute(
            "UPDATE imported_sessions SET category = ?, "
            "  category_set_by = ?, "
            "  category_set_at = datetime('now') WHERE id = ?",
            (category, set_by, imported_id),
        )
        self._safe_commit()
        return cur.rowcount > 0

    def category_stats(self) -> dict:
        """Headline counts by category."""
        rows = self._conn.execute(
            "SELECT COALESCE(NULLIF(category,''),'(unset)') AS cat, "
            "  COUNT(*) AS n FROM imported_sessions GROUP BY cat "
            "ORDER BY n DESC"
        ).fetchall()
        by_cat = {r["cat"]: r["n"] for r in rows}
        # Also break down by source within unclassified — that's the
        # population the LLM classifier needs to chew on.
        unclassified_by_source = {}
        for r in self._conn.execute(
            "SELECT source, COUNT(*) AS n FROM imported_sessions "
            "WHERE COALESCE(category,'') IN ('','unclassified') "
            "GROUP BY source ORDER BY n DESC"
        ).fetchall():
            unclassified_by_source[r["source"]] = r["n"]
        return {
            "by_category": by_cat,
            "unclassified_by_source": unclassified_by_source,
        }

    def list_unclassified_sessions(self, *, source=None,
                                    limit=200) -> list:
        """For the LLM classifier batch path. Returns sessions where
        category is empty or 'unclassified', filtered by source if
        given. Ordered by started_at DESC so newest hit first.
        """
        sql = ("SELECT id, source, source_path, project, cwd, "
               "started_at, metadata_json "
               "FROM imported_sessions "
               "WHERE COALESCE(category,'') IN ('','unclassified')")
        params: list = []
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in
                self._conn.execute(sql, params).fetchall()]

    # ── end Slice 14.7.3 ────────────────────────────────────────

    def sensitivity_stats(self) -> dict:
        """Headline counts by tier across imported_sessions."""
        rows = self._conn.execute(
            "SELECT COALESCE(sensitivity,'(unset)') AS tier, "
            "COUNT(*) AS n FROM imported_sessions GROUP BY tier"
        ).fetchall()
        return {r["tier"]: r["n"] for r in rows}

    def scan_outbound_text_for_sensitive(self, text: str) -> list:
        """Slice 13 CP3: scan text that's about to LEAVE the Pi (a
        sibling-dispatch prompt + context) for leak risk.

        Two kinds of hit:
          - 'pattern': a credential / PII regex from
            DEFAULT_SENSITIVE_PATTERNS matched
          - 'confidential_session_ref': the text contains the id of
            an imported_session tiered confidential/restricted —
            i.e. the overseer is about to ship confidential context
            to a sibling (which sends it to the Anthropic API)

        Returns a list of hit dicts. Empty list = safe to dispatch."""
        import re
        if not text:
            return []
        hits = []
        for name, pat, desc in self.DEFAULT_SENSITIVE_PATTERNS:
            try:
                if re.search(pat, text):
                    hits.append({"kind": "pattern", "name": name,
                                 "desc": desc})
            except re.error:
                continue
        # References to confidential/restricted sessions by id.
        try:
            conf_rows = self._conn.execute(
                "SELECT id, sensitivity FROM imported_sessions "
                "WHERE sensitivity IN ('confidential','restricted')"
            ).fetchall()
            for r in conf_rows:
                sid = r["id"]
                if sid and len(sid) > 8 and sid in text:
                    hits.append({
                        "kind": "confidential_session_ref",
                        "name": sid, "desc": f"{r['sensitivity']} session id",
                    })
        except Exception:
            pass
        return hits
