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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (raw_pointer_id) REFERENCES raw_pointers(id)
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
-- Single ongoing thread for v1 (no conversation/thread separation).
-- All messages stored append-only. The chat handler builds context
-- from working_memory + recent gists + last N chat_messages.

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        self._conn.executescript(OVERSEER_SCHEMA_SQL)
        self._safe_commit()
        self._migrate_3f5()
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
            seeds = [
                # (match_type, pattern, tier, retention, priority, note)
                ("cwd_like", "%ProjectX%", "restricted", "no-import", 300,
                 "ProjectX acquisition — deal IP, never import raw"),
                ("cwd_like", "%ClientA%", "confidential", "gist-and-drop",
                 200, "ClientA work — clinical / HIPAA-adjacent"),
                ("cwd_like", "%/home/workuser%", "confidential",
                 "gist-and-drop", 200, "an employer tenant work"),
                ("cwd_like", "%workuser%", "confidential",
                 "gist-and-drop", 180,
                 "Work-computer user profile path"),
                ("cwd_like", "%an employer%", "confidential",
                 "gist-and-drop", 180, "an employer doc paths"),
                ("cwd_like", "%exec-email%", "confidential",
                 "gist-and-drop", 200, "COO email parsing — exec comms"),
                # Forward-looking: healthcare-contractor orgs.
                ("cwd_like", "%\\rhd%", "confidential", "gist-and-drop",
                 160, "Contractor A contractor work"),
                ("cwd_like", "%\\hhs%", "confidential", "gist-and-drop",
                 160, "Contractor B contractor work"),
                ("cwd_like", "%nahm%", "confidential", "gist-and-drop",
                 160, "Contractor C contractor work"),
            ]
            for mt, pat, tier, ret, pri, note in seeds:
                self._conn.execute(
                    "INSERT INTO sensitivity_rules "
                    "(match_type, pattern, tier, retention_policy, "
                    " priority, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (mt, pat, tier, ret, pri, note),
                )
            self._safe_commit()
            log.info("_migrate_13_sensitivity: seeded %d rules",
                     len(seeds))
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

    # ── summaries_gist ──────────────────────────────────────────

    def add_gist(self, body, *, period_label="", period_start=None,
                 period_end=None, confidence="med", raw_pointer_id=None,
                 tags=None):
        cur = self._conn.execute(
            "INSERT INTO summaries_gist (period_label, period_start, period_end, "
            "body, confidence, raw_pointer_id) VALUES (?, ?, ?, ?, ?, ?)",
            (period_label, period_start, period_end, body,
             _norm_confidence(confidence), raw_pointer_id),
        )
        self._safe_commit()
        gid = cur.lastrowid
        self.tag_many("summaries_gist", gid, tags)
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

    def append_chat_message(self, *, role, content, backend="", model="",
                            latency_ms=0, cost_usd=0.0,
                            prompt_tokens=0, response_tokens=0,
                            metadata=None,
                            answered_by="",
                            escalation_reason=""):
        cur = self._conn.execute(
            "INSERT INTO chat_messages (role, content, backend, model, "
            "latency_ms, cost_usd, prompt_tokens, response_tokens, "
            "metadata_json, answered_by, escalation_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (role, content, backend, model, int(latency_ms),
             float(cost_usd), int(prompt_tokens), int(response_tokens),
             json.dumps(metadata or {}),
             answered_by, escalation_reason),
        )
        self._safe_commit()
        return cur.lastrowid

    def count_consecutive_router_turns(self, limit=8) -> int:
        """Slice 14.7: count assistant turns at the end of the chat
        thread that were answered by the router (answered_by='router')
        without an overseer escalation breaking the streak. Used by
        the router to escalate when it's been answering on the same
        thread for too long without resolution."""
        rows = self._conn.execute(
            "SELECT role, answered_by FROM chat_messages "
            "WHERE role = 'assistant' "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        n = 0
        for r in rows:
            if (r["answered_by"] or "") == "router":
                n += 1
            else:
                break
        return n

    def recent_chat_messages(self, limit=40, *, include_files=True):
        """Most-recent N rows in chronological order. When include_files
        is True (default), each row gets an `attachments` list populated
        from chat_message_files. Slice 8."""
        rows = self._conn.execute(
            "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?",
            (int(limit),),
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

    def chat_message_count(self):
        return self._conn.execute(
            "SELECT COUNT(*) FROM chat_messages"
        ).fetchone()[0]

    def clear_chat(self):
        # FK ON DELETE CASCADE only fires when foreign_keys pragma is
        # ON. SQLite defaults it OFF. Delete files explicitly first so
        # a 'Clear thread' on an existing install doesn't leave orphan
        # chat_message_files rows.
        self._conn.execute("DELETE FROM chat_message_files")
        self._conn.execute("DELETE FROM chat_messages")
        self._safe_commit()

    def compress_chat_replace(self, *, old_ids, summary_content,
                              created_at=None, metadata=None):
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
                    "(role, content, backend, model, latency_ms, "
                    "cost_usd, prompt_tokens, response_tokens, "
                    "metadata_json, created_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("system", summary_content, "compress-internal",
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
            "related_id=excluded.related_id, action_url=excluded.action_url",
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
        across sessions."""
        if not name:
            return None
        row = self._conn.execute(
            "SELECT * FROM overseer_people WHERE LOWER(name) = LOWER(?)",
            (name.strip(),),
        ).fetchone()
        return dict(row) if row else None

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
                "SELECT * FROM overseer_people ORDER BY {} DESC NULLS LAST "
                "LIMIT ? OFFSET ?".format(order_by),
                (int(limit), int(offset)),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = self._conn.execute(
                "SELECT * FROM overseer_people ORDER BY {} DESC "
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
            "SELECT * FROM overseer_people WHERE "
            "  LOWER(name) LIKE LOWER(?) "
            "  OR LOWER(display_name) LIKE LOWER(?) "
            "  OR LOWER(online_handles_json) LIKE LOWER(?) "
            "  OR LOWER(areas_of_expertise_json) LIKE LOWER(?) "
            "  OR LOWER(tags_json) LIKE LOWER(?) "
            "  OR LOWER(notes) LIKE LOWER(?) "
            "ORDER BY last_interacted_at DESC, updated_at DESC "
            "LIMIT ?",
            (like, like, like, like, like, like, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_person(self, *, name, display_name="", online_handles=None,
                    social_links=None, areas_of_expertise=None,
                    notes="", tags=None, last_interacted_at=None,
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
            " tags_json, last_interacted_at, created_by_agent, "
            " created_by_session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name.strip(),
                display_name.strip() if display_name else "",
                json.dumps(online_handles or []),
                json.dumps(social_links or []),
                json.dumps(areas_of_expertise or []),
                notes.strip() if notes else "",
                json.dumps(tags or []),
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
                       notes_replace=None, tags=None,
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

        total = cur.execute(
            "SELECT COUNT(*) AS n FROM overseer_people"
        ).fetchone()["n"]

        added_24h = cur.execute(
            "SELECT COUNT(*) AS n FROM overseer_people "
            "WHERE created_at >= datetime('now', '-1 day')"
        ).fetchone()["n"]
        added_7d = cur.execute(
            "SELECT COUNT(*) AS n FROM overseer_people "
            "WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()["n"]

        orphans = cur.execute(
            "SELECT COUNT(*) AS n FROM overseer_people p "
            "WHERE NOT EXISTS ("
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
            "FROM overseer_people ORDER BY created_at DESC LIMIT 10"
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
        """ISO timestamp of midnight-local-time, expressed as UTC.
        Used by sibling_* methods so the daily cap calendar matches the
        user's day (same convention as Slice 5.5's DailyBudget reset)."""
        from datetime import datetime, timezone, timedelta
        offset_min = 0
        try:
            raw = self.get_overseer_state("local_tz_offset_minutes")
            if raw is not None:
                offset_min = int(raw)
        except Exception:
            pass
        now_local = (datetime.now(timezone.utc)
                     + timedelta(minutes=offset_min))
        day_start_local = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_local - timedelta(minutes=offset_min)
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
        user_row = self._conn.execute(
            "SELECT id, content, created_at FROM chat_messages "
            "WHERE role = 'user' AND created_at <= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (row.get("created_at"),),
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
        ("cwd_lower_contains",  "ClientA",              "work"),
        ("cwd_lower_contains",  "workuser",      "work"),
        ("cwd_lower_contains",  "clientb",          "work"),
        ("cwd_lower_contains",  "employer",            "work"),
        ("cwd_lower_contains",  "employer",           "work"),
        ("cwd_lower_contains",  "infusion",          "work"),
        ("cwd_lower_contains",  "ClientD",    "work"),
        ("cwd_lower_contains",  "ProjectX",               "work"),
        ("cwd_lower_contains",  "sidegig",            "work"),
        # cortex bucket — Cortex itself + its sibling repos
        ("cwd_lower_contains",  "cortex-pet",        "cortex"),
        ("cwd_lower_contains",  "cortex-link",       "cortex"),
        ("cwd_lower_contains",  "cortex-mcp",        "cortex"),
        ("cwd_lower_contains",  "cortex-desktop",    "cortex"),
        ("cwd_lower_contains",  "cortex-core",       "cortex"),
        ("cwd_lower_contains",  "cortex",            "cortex"),
        # personal — Tory's own ventures and exploration projects
        ("cwd_lower_contains",  "openmuscle",        "personal"),
        ("cwd_lower_contains",  "open-muscle",       "personal"),
        ("cwd_lower_contains",  "flexgrid",          "personal"),
        ("cwd_lower_contains",  "truthsea",          "personal"),
        ("cwd_lower_contains",  "uap-gerb",          "personal"),
        ("cwd_lower_contains",  "uap_gerb",          "personal"),
        ("cwd_lower_contains",  "ufosint",           "personal"),
        ("cwd_lower_contains",  "polymarket",        "personal"),
        ("cwd_lower_contains",  "monofonic",         "personal"),
        ("cwd_lower_contains",  "smallcode",         "personal"),
        ("cwd_lower_contains",  "godisgood",         "personal"),
        ("cwd_lower_contains",  "lemon",             "personal"),
        # project field also worth checking
        ("project_lower_contains", "openmuscle",     "personal"),
        ("project_lower_contains", "cortex",         "cortex"),
        ("project_lower_contains", "ClientA",           "work"),
        ("project_lower_contains", "ProjectX",            "work"),
        ("project_lower_contains", "client-d", "work"),
    ]

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
        for kind, pattern, cat in self._CATEGORY_RULES:
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
