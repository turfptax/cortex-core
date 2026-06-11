"""Overseer chat tools — Slice 10 origin, evolved through 2026-05-27.

Tool surface the overseer can call during a chat turn (or journal
step) to fetch live data and take audited write actions. Mirrors the
F1 MCP surface external AIs use, plus overseer-specific writes that
have no external equivalent.

Surfaces by category (~35 tools total):
- **Read (per-table)**: get_recent_human_journal, get_recent_overseer_journal,
  search_notes, get_notes_by_tag, get_recent_sessions, get_session_detail,
  list_active_projects, get_project_detail, get_open_questions,
  get_known_blindspots, get_pending_interpretations, get_temporal_narrative,
  search_people, get_recent_patterns, get_recent_drift.
- **Read (F1 unified, 2026-05-27)**: cortex_search (cross-kind, layered
  returns) + cortex_resolve_token (drill any working_memory token).
  These mirror the MCP surface so overseer can run its own
  audit-before-claim rule without asking Tory to verify externally.
- **Writes (Slice 9.6 CP2)**: update_project_status, create_project,
  create_question, update_question_lifecycle. Auditable mutations —
  not the same as bypassing review queues, but real state change.
- **Notification system**: emit_notification (with optional
  actions_json), get_pending_notification_responses,
  mark_notification_responses_processed.
- **Sibling/B-agent dispatch**: dispatch_sibling (Cat A, Claude Code),
  rate_sibling_result, dispatch_b_<name> (Cat B specialists),
  accept_c_promotion (B→C graduation).
- **Evidence + sensitivity**: file_evidence, propose_project_merge,
  redact_imported_session, scan_for_sensitive_content,
  redact_human_journal.
- **Chat management**: compress_chat, redact_chat_attachment,
  delete_chat_message.

Each tool follows OpenAI's function-calling schema (which OpenRouter
normalizes to Anthropic's tool_use format for Opus and to the
appropriate native format for any provider the resolver picks). The
dispatcher takes (name, args) -> str and returns a JSON-serialized
result the model can read on the next turn.

Cost shape: each tool call adds one LLM round-trip. MAX_TOOL_ITER
bounds the loop so a confused model can't burn budget indefinitely.
Per-tool result is truncated to MAX_TOOL_RESULT_CHARS so a fat
result doesn't blow the prompt budget.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("plugin.overseer.chat_tools")


# ── Tool definitions (OpenAI function-calling schema) ────────────

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_recent_human_journal",
            "description": (
                "Read the user's most recent journal entries — the textarea "
                "entries the user writes themselves, NOT the overseer's "
                "tick reflections. Use this when the user references their own "
                "writing, asks 'did you see what I wrote', or when the "
                "static context's coverage is too small."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (default 10, max 50).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_overseer_journal",
            "description": (
                "Read the overseer's own first-person tick journal — the "
                "reflections it writes each background tick. Use to check "
                "your own past observations or a thread you started "
                "earlier."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (default 10).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": (
                "Full-text search across the user's notes table. Returns "
                "matching notes with id, created_at, tags, and content "
                "preview. Use for 'did I write about X', 'what notes "
                "tagged Y', specific topic recall."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search string. Matched against note content (LIKE %query%).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, max 50).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_notes_by_tag",
            "description": (
                "Return notes filtered by a single tag. Tags are "
                "comma-separated in the source row; this matches if the "
                "tag appears anywhere in the column."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                    },
                },
                "required": ["tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_sessions",
            "description": (
                "List recent sessions (Claude Code conversations on the "
                "user's machines). Returns id, ai_platform, hostname, "
                "started_at, ended_at, summary, projects."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_detail",
            "description": (
                "Full detail for a single session by id, including its "
                "summary, attached notes, activities."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_active_projects",
            "description": (
                "List active projects from the project_summaries table, "
                "sorted by total_minutes_active desc by default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max projects (default 20).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_detail",
            "description": (
                "Get a project's narrative + stats + recent sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact project name.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_questions",
            "description": (
                "Get the overseer's standing open questions about the "
                "user, with linked evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max questions (default 10).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_known_blindspots",
            "description": (
                "List the overseer's known blindspots — meta-honesty "
                "entries that surface when the overseer is reasoning in "
                "a domain it knows it's been wrong about before."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_interpretations",
            "description": (
                "List interpretations awaiting the user's review (gists, "
                "themes, episodes, blindspots, drift, patterns)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "Filter by kind: gist, theme, episode, blindspot, drift, pattern. Omit for all.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_temporal_narrative",
            "description": (
                "Read a temporal narrative (daily/weekly/monthly/yearly). "
                "period_label format: 'YYYY-MM-DD' for daily, 'YYYY-Www' "
                "for weekly (e.g. '2026-W19'), 'YYYY-MM' for monthly, "
                "'YYYY' for yearly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "yearly"],
                    },
                    "period_label": {"type": "string"},
                },
                "required": ["kind", "period_label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_people",
            "description": (
                "Look up people by name or expertise tags from the "
                "overseer_people table."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_patterns",
            "description": (
                "Recent observations of recurring patterns the overseer "
                "has noticed across the user's work / behavior."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_drift",
            "description": (
                "Recent drift observations — places where the user has "
                "started/stopped/shifted a behavior or framing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10).",
                    },
                },
            },
        },
    },
    # ── Slice 9.3: read-side of sibling dispatch ───────────────────
    # Paired with dispatch_sibling (write) so the overseer can integrate
    # sibling work WITHIN a chat turn instead of waiting for a tick.
    # Use after dispatching to check whether the sibling has completed
    # the task yet; also use opportunistically to see if any siblings
    # have completed work the overseer hasn't rated yet (the
    # unrated_only filter is the natural inbox view).
    {
        "type": "function",
        "function": {
            "name": "get_recent_sibling_results",
            "description": (
                "Recently completed/failed/rejected sibling tasks — "
                "the read counterpart to dispatch_sibling. Use this to "
                "(a) check whether a task you dispatched has been "
                "completed yet, (b) read the result text + the "
                "sibling's reciprocal grade of your dispatch quality, "
                "and (c) find completed tasks you haven't rated yet so "
                "you can close the audit loop. Each row includes "
                "result_text (full, never compacted), the sibling's "
                "dispatch_quality_rating + notes, and the actual model "
                "the sibling used. If unrated_only=true, filters to "
                "tasks where you haven't yet set quality_rating — your "
                "inbox of work-awaiting-your-read."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10).",
                    },
                    "unrated_only": {
                        "type": "boolean",
                        "description": (
                            "If true, return only completed tasks you "
                            "haven't rated yet. Useful as your inbox."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rate_sibling_result",
            "description": (
                "Rate a completed sibling task's result quality (1-5) "
                "and optionally flag it as a dataset_candidate for "
                "future Category C agent training. Use this AFTER "
                "reading the result via get_recent_sibling_results "
                "and integrating it into your reasoning. Rating closes "
                "the audit loop and feeds the long-term flywheel that "
                "trains specialized agents on (prompt, result, rating) "
                "triples. Bias warning: you will be tempted to rate "
                "work that confirms your prior read higher; the "
                "reciprocal grading on dispatch quality is one "
                "mitigation, but the most honest mitigation is to "
                "pre-commit to a rating ceiling BEFORE reading the "
                "result. Quote your pre-commit in the notes field."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "The id of the completed task.",
                    },
                    "rating": {
                        "type": "integer",
                        "description": "1 (useless) to 5 (load-bearing).",
                    },
                    "notes": {
                        "type": "string",
                        "description": (
                            "Why you rated it that way. Specifically: "
                            "what part of the result did real work for "
                            "you, what part was restatement, and "
                            "whether you arrived at the same point "
                            "independently."
                        ),
                    },
                    "dataset_candidate": {
                        "type": "boolean",
                        "description": (
                            "Flag this (prompt, context, result) "
                            "triple as exemplar training data for "
                            "future specialized agents. Only true for "
                            "ratings >= 4."
                        ),
                    },
                },
                "required": ["task_id", "rating"],
            },
        },
    },
    # ── Slice 9.3: sibling dispatch — the FIRST write tool ─────────
    # All prior tools in this file are read-only inspection. This one
    # writes to sibling_tasks. Distinguished by name + tool description
    # so the model knows it's qualitatively different.
    {
        "type": "function",
        "function": {
            "name": "compress_chat",
            "description": (
                "Fold older messages in THIS chat thread into a single "
                "Sonnet-summarized prefix. Use when you notice the chat "
                "history is bloating your per-turn context cost — "
                "specifically when (a) the thread has 20+ turns and the "
                "older half is no longer actively load-bearing for the "
                "current topic, or (b) Tory has shifted topic and the "
                "prior context is now noise, or (c) you've been calling "
                "many tools and the tool-result text is dominating "
                "history.\n\nDO NOT use this if: the conversation is "
                "active and recent context matters, the thread is < 15 "
                "turns, or you're mid-decision and need the original "
                "framing intact. Compression is destructive — the "
                "originals are deleted and replaced with the summary.\n\n"
                "Cost: ~$0.01-0.02 per compression (Sonnet). The "
                "compression summary is preserved as a synthetic system "
                "message at the head of the thread; future turns read "
                "it as context. Tool-call audit (which tools you "
                "called) IS preserved in the summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keep_recent": {
                        "type": "integer",
                        "description": (
                            "How many of the most recent turns to keep "
                            "raw. Default 12. Minimum 2."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_sibling",
            "description": (
                "Dispatch a task to a sibling agent (currently: a Claude "
                "Code session on Tory's PC). Use this when you need a "
                "fresh perspective on something in your own state — "
                "specifically when you're (a) uncertain whether you're "
                "pattern-matching too hard on a frame, (b) want a "
                "second opinion on whether a theme deserves [high] "
                "confidence given recent evidence, or (c) have a "
                "concrete question that one round-trip can resolve.\n\n"
                "DO NOT use this to: ask generic LLM questions (free "
                "via your other channels), do routine summarization "
                "(no sibling needed), or as small-talk. Each dispatch "
                "costs real money on the caller's Anthropic budget and "
                "burns from your daily dispatch cap (currently 20/day, "
                "checkable via your dispatch_stats freshness signal).\n\n"
                "Returns the task id. The sibling will claim and "
                "complete it asynchronously; you'll see the result on "
                "a future tick via the sibling_recent surface, with an "
                "optional reciprocal rating where the sibling grades "
                "the quality of your dispatch (specifically to prevent "
                "you rating your own ideas back to yourself as valid)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "What you want the sibling to do. Concrete "
                            "and bounded. Example: 'Re-read "
                            "human_journal id=4 and tell me if I'm "
                            "overfitting the spine-vs-cover-story "
                            "frame on FlexGrid V3.' Bad example: "
                            "'What do you think of Open Muscle?'"
                        ),
                    },
                    "context": {
                        "type": "object",
                        "description": (
                            "Any additional context the sibling needs "
                            "(excerpts, IDs of relevant rows, links to "
                            "your prior reasoning). Stored verbatim "
                            "in context_json on the task row."
                        ),
                    },
                    "cost_budget_usd": {
                        "type": "number",
                        "description": (
                            "Max cost the sibling should spend on this "
                            "task. Default 0.50 USD. Use lower for "
                            "small fact-checks, higher for genuinely "
                            "open-ended judgment work."
                        ),
                    },
                    "task_type": {
                        "type": "string",
                        "enum": ["judgment", "synthesis", "fact-check"],
                        "description": (
                            "What kind of task. judgment=needs a real "
                            "agent's read (default; targets Claude "
                            "Code). synthesis=summarize/rewrite. "
                            "fact-check=DB lookups + verify."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    # ── Slice 9.6 CP2 (2026-05-19): write tools ────────────────────
    {
        "type": "function",
        "function": {
            "name": "update_project_status",
            "description": (
                "Change a project's status (active | dormant | archived). "
                "Use when Tory has stopped working on a project, or after "
                "a stale-project notification response indicates he wants "
                "it archived/marked-dormant rather than 'touched'. "
                "Idempotent — no-op if status already matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Project tag (slug)."},
                    "status": {
                        "type": "string",
                        "description": "active | dormant | archived",
                    },
                },
                "required": ["tag", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": (
                "Create a new project record. Use sparingly — most "
                "projects auto-emerge from Claude Code session ingestion. "
                "Use this when Tory has named a project verbally / in "
                "chat but no session row has yet seeded it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "name": {"type": "string"},
                    "status": {"type": "string", "description": "active|dormant|archived (default active)"},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_question",
            "description": (
                "Add a new open_question to your interpretive layer. Use "
                "when a recurring concern surfaces that you want to track "
                "across sessions / weeks. Not for one-off curiosities. "
                "If a similar question already exists, file evidence to "
                "it instead via file_evidence (not yet a tool)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "body": {"type": "string"},
                    "confidence": {"type": "string", "description": "high|med|low"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_question_lifecycle",
            "description": (
                "Move an open_question through its lifecycle: dormant | "
                "active | partially_answered | resolved | abandoned. "
                "Use this to close stale questions or to revive ones "
                "that have new evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "integer"},
                    "lifecycle": {"type": "string"},
                },
                "required": ["question_id", "lifecycle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "redact_chat_attachment",
            "description": (
                "Remove an attached file from a chat message. Pass "
                "either file_id (single file) or message_id (all "
                "attachments on the message). The file itself is left "
                "on disk; only the DB linkage is removed so it stops "
                "appearing in chat history + LLM prompts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "integer"},
                    "message_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_chat_message",
            "description": (
                "DESTRUCTIVE: delete a single chat_messages row and all "
                "its attachments. Use only when Tory has explicitly "
                "asked for a message to be scrubbed (privacy, "
                "embarrassment, accidental paste). Cannot be undone "
                "from the DB layer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer"},
                },
                "required": ["message_id"],
            },
        },
    },
    # ── Slice 9.6 CP3 (2026-05-19): notification emit + responses ─
    {
        "type": "function",
        "function": {
            "name": "emit_notification",
            "description": (
                "Send Tory a notification with optional custom action "
                "buttons. Appears in the Hub Bell tab. Use for things "
                "you want him to see soon but not immediately interrupt "
                "for. Custom action kinds: 'free_text' (opens a "
                "textarea for free reply), 'yes_no' (two-button binary), "
                "'dispatch_sibling' (Tory clicks → creates a sibling "
                "task seeded with this notification's context), "
                "'archive_project'/'mark_dormant'/etc (predefined CRUD "
                "you'll act on when reading the response).\n\n"
                "Tory's response lands in pending_notification_responses, "
                "surfaced in your freshness on next tick, fetchable via "
                "get_pending_notification_responses. Be intentional — "
                "the Bell tab's noise floor is real; emit only when "
                "you want a structured reply, not as broadcast."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "description": "info | warn | important",
                    },
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "rule_key": {
                        "type": "string",
                        "description": "Optional dedup key (auto-generated if omitted).",
                    },
                    "related_table": {"type": "string"},
                    "related_id": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "description": (
                            "Custom action buttons. Each: "
                            "{label, kind, payload?}. Example: "
                            "[{label: 'Yes archive', kind: 'archive_project', "
                            "payload: {tag: 'openmuscle-flexgrid'}}, "
                            "{label: 'No keep active', kind: 'yes_no', "
                            "payload: {value: 'no'}}]"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "kind": {"type": "string"},
                                "payload": {"type": "object"},
                            },
                            "required": ["label", "kind"],
                        },
                    },
                },
                "required": ["severity", "title"],
            },
        },
    },
    # ── Slice 9.7 (2026-05-19/20): synthesis primitives ────────────
    # Per overseer's explicit spec ("synthesis power, not raw write
    # power"). Three tools chosen by overseer when given a blank
    # check; rejected the entity-CRUD expansion in favor of these.
    {
        "type": "function",
        "function": {
            "name": "file_evidence",
            "description": (
                "File a piece of evidence to an open question to "
                "build its evidence trail. Source can be ANY artifact "
                "table — extends the existing gist-only file-evidence "
                "flow to cover notes / sessions / human_journal / "
                "your own journal reflections / chat messages.\n\n"
                "Use this as your PRIMARY synthesis primitive. If "
                "you can name 'evidence E supports question Q', file "
                "it. The map between projects and questions emerges "
                "implicitly from filed evidence — if 8 pieces of "
                "evidence on question Q come from project P's sessions, "
                "the link is structural, no junction table needed.\n\n"
                "Idempotent — re-filing the same (question, evidence) "
                "pair is a no-op. Filing 'answers' will move an active "
                "question to 'partially_answered'; never auto-resolves "
                "(that's Tory's call)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "integer"},
                    "source_table": {
                        "type": "string",
                        "description": (
                            "Which table the evidence row lives in. "
                            "Common: summaries_gist | summaries_theme "
                            "| summaries_episode | imported_sessions "
                            "| chat_messages | overseer_journal | "
                            "human_journal_entries | notes."
                        ),
                    },
                    "source_id": {"type": "integer"},
                    "stance": {
                        "type": "string",
                        "description": (
                            "supports | complicates | answers | "
                            "reframes (default 'supports')"
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "One-sentence why this evidence routes "
                            "to this question. Load-bearing — without "
                            "it the audit trail is opaque."
                        ),
                    },
                    "confidence": {
                        "type": "string",
                        "description": "high|med|low — your confidence in the routing call",
                    },
                },
                "required": ["question_id", "source_table", "source_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_project_merge",
            "description": (
                "Surface a proposed project merge to Tory's Hub "
                "Insights queue. Use when you notice two projects "
                "look like duplicates, aliases, or one is a "
                "sub-project of the other — you see 83 active "
                "projects on every working memory rebuild and you've "
                "named that 10-15 are probably duplicates.\n\n"
                "DOES NOT execute the merge. Writes one row to "
                "pending_interpretations with kind='merge_proposal' "
                "for Tory to accept/reject. The rationale field is "
                "load-bearing: explain WHY you think they're the "
                "same work, ideally citing specific sessions or "
                "themes that overlap.\n\n"
                "Dedup is automatic — re-proposing the same merge "
                "won't double-up. Convention: put the keeper tag in "
                "tag_a, the merge-source in tag_b."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag_a": {
                        "type": "string",
                        "description": "Keeper project tag (the canonical one)",
                    },
                    "tag_b": {
                        "type": "string",
                        "description": "Project tag to be merged into tag_a",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": (
                            "Why they look like the same work. "
                            "Cite specific evidence: 'both had 5 "
                            "sessions in the OpenMuscle-Software repo "
                            "in 2026-05-14..16; same Claude Code cwd'."
                        ),
                    },
                },
                "required": ["tag_a", "tag_b", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "redact_imported_session",
            "description": (
                "Scrub an imported AI-conversation session (Claude "
                "Code, ChatGPT, Grok, etc.) from cortex. Two modes:\n\n"
                "- mark_redacted (DEFAULT): replaces the on-disk "
                "  .jsonl with a [REDACTED] placeholder line; keeps "
                "  the imported_sessions row + metadata (timestamps, "
                "  project, source) so session counts and project "
                "  summaries don't lie. The redacted_at timestamp is "
                "  set. Existing gist(s) generated from this session "
                "  are NOT scrubbed — they live independently and "
                "  contain Sonnet's summary of the content. If the "
                "  gist itself is sensitive, surface it for separate "
                "  handling.\n"
                "- delete_row: destructive. Removes the .jsonl file "
                "  AND the imported_sessions row AND any "
                "  processed_imported_sessions record. The session "
                "  count drops. Existing gists still survive (they "
                "  point at a now-dead source).\n\n"
                "Use sparingly. When Tory asks for a session to be "
                "redacted, prefer mark_redacted unless he explicitly "
                "says 'delete' or the row should never have been "
                "imported in the first place."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "imported_id": {
                        "type": "string",
                        "description": "The imported_sessions.id (e.g. 'claude-code:UUID')",
                    },
                    "mode": {
                        "type": "string",
                        "description": "mark_redacted (default) | delete_row",
                    },
                },
                "required": ["imported_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_for_sensitive_content",
            "description": (
                "Scan imported sessions for sensitive content "
                "candidates Tory may want to redact. Regex-based, "
                "fast, no LLM cost. Default patterns cover: API "
                "keys (OpenAI, Anthropic, GitHub PATs, AWS, Stripe, "
                "Slack), Bearer/JWT tokens, PEM private keys, SSH "
                "keys, credit cards, US SSNs, US phone numbers, "
                "inline password/secret assignments.\n\n"
                "Pass extra_patterns as a list of [name, regex, "
                "description] triples for project-specific things "
                "(Tory's address, names of people he wants kept "
                "private, internal API URLs, etc.).\n\n"
                "Returns per-session match summaries. Workflow: scan "
                "→ emit_notification per session-with-matches → Tory "
                "clicks Redact/Keep → on next tick fetch responses + "
                "call redact_imported_session for the ones marked. "
                "Skips sessions already redacted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Optional filter: only scan sessions "
                            "with this source (e.g. 'claude-code', "
                            "'grok-com', 'chatgpt')"
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Optional ISO date — only scan sessions "
                            "started on or after this date"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max sessions to scan (default 20)",
                    },
                    "extra_patterns": {
                        "type": "array",
                        "description": (
                            "Optional list of [name, regex, "
                            "description] triples added on top of "
                            "defaults"
                        ),
                        "items": {"type": "array"},
                    },
                    "use_defaults": {
                        "type": "boolean",
                        "description": "Include default patterns (default true)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "redact_human_journal",
            "description": (
                "DESTRUCTIVE: delete a human_journal_entries row. Use "
                "ONLY when Tory has explicitly asked for a journal "
                "entry to be pulled from your view (privacy, regret, "
                "wanted-to-rewrite, accidental). The row + its "
                "timestamp variants are removed.\n\n"
                "NOT scrubbed: any temporal narratives (daily / "
                "weekly / monthly / yearly rollups) that may have "
                "folded this entry's content into a summary "
                "already. Those would need manual regeneration. "
                "Flag this consequence to Tory when redacting if "
                "the entry is older than a day."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "integer"},
                },
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_notification_responses",
            "description": (
                "Fetch Tory's unread responses to notifications you "
                "emitted. Returns the full notification context + his "
                "response payload + action kind clicked. Call this when "
                "your freshness shows pending_notification_responses > 0. "
                "After you've acted on a response, call "
                "mark_notification_responses_processed with the response "
                "ids so they don't re-surface."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "max rows (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_notification_responses_processed",
            "description": (
                "Mark a list of notification_response ids as read so "
                "they stop appearing in get_pending_notification_responses. "
                "Call after acting on the response (e.g. you updated the "
                "project status per Tory's reply)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "response_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["response_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accept_c_promotion",
            "description": (
                "Slice 10 CP5 (2026-05-20): accept a B-agent C-"
                "graduation proposal from Tory. Creates the c_agents "
                "row, freezing the B parent's system_prompt and model "
                "at promotion time. Call this ONLY after Tory has "
                "clicked 'Promote to C' on a c-graduation notification "
                "(check pending_notification_responses for actions of "
                "kind='promote_b_to_c'). C runs on a schedule "
                "(cadence_minutes; default 1440 = 24h) and shares the "
                "B's snapshot-builder, but its audit rows carry "
                "target='c-agent:<name>' instead of 'b-agent:<name>'. "
                "Idempotent: returns error if c_agent_name already "
                "exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "b_agent_name": {
                        "type": "string",
                        "description": "Parent B agent name (e.g. "
                                       "'theme_check'). Must exist in "
                                       "the live B registry.",
                    },
                    "c_agent_name": {
                        "type": "string",
                        "description": "Name for the new C agent "
                                       "(e.g. 'theme-check-daily'). "
                                       "Must be unique across c_agents.",
                    },
                    "cadence_minutes": {
                        "type": "integer",
                        "description": "Run interval in minutes. "
                                       "Default 1440 (24h).",
                    },
                },
                "required": ["b_agent_name", "c_agent_name"],
            },
        },
    },
    # ── Phase 1 / Audit follow-up (2026-05-27): F1 reader surface ──
    # External AIs already have cortex_search + cortex_overseer_detail
    # via the MCP wrapper. The 24h activity bundle audit showed
    # overseer's own chat tool palette was missing them — 13 of 15
    # runs used ZERO tools because the "audit-before-claim" rule had
    # no clean fetching primitive. These two tools close that gap so
    # overseer can run its own F1 surface from chat.
    {
        "type": "function",
        "function": {
            "name": "cortex_search",
            "description": (
                "Unified substring search across the interpretive "
                "corpus — the same F1 surface external AIs have via "
                "MCP. Returns layered hits per "
                "three_layer_architecture_design_seed.md: "
                "`abstractions` (Layer 1: themes, patterns, drift, "
                "future_notes, journal entries, narratives, episodes, "
                "questions, blindspots, human journal), `gists` "
                "(Layer 2: per-session summaries, each with `raw_id` "
                "when its source is an imported_session), and "
                "`raw_refs` (Layer 3 pointers, deduplicated). Each "
                "hit carries a drill `token` (e.g. `g:3141`, `t:6`, "
                "`gp:1`) that resolves via cortex_resolve_token.\n\n"
                "Use when: a topic could span multiple kinds and you "
                "want one call instead of guessing which "
                "per-table get_recent_X to invoke; the persona's "
                "audit-before-claim rule demands a fetch and you "
                "don't know the exact row ID yet; a recommendation "
                "depends on what the corpus currently knows.\n\n"
                "Pull events are logged with surface='chat:cortex_"
                "search' so your own queries feed back into the "
                "refinement-loop signal (drill-past ratios per "
                "gist_prompts row).\n\n"
                "DON'T use for: notes-only search — use search_notes "
                "(different table, kept for compat). Drilling a "
                "specific token you already know — use cortex_"
                "resolve_token. Routine context already in your "
                "context block — don't burn a tool call to restate "
                "what's already there."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": (
                            "Substring to match (case-insensitive). "
                            "Minimum 2 characters."
                        ),
                    },
                    "kinds": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated subset of: "
                            "gist, theme, episode, pattern, drift, "
                            "note, journal, narrative, question, "
                            "blindspot, human. Empty = all 11."
                        ),
                    },
                    "limit_per_kind": {
                        "type": "integer",
                        "description": (
                            "Max hits per kind (default 5, max 50)."
                        ),
                    },
                    "limit_total": {
                        "type": "integer",
                        "description": (
                            "Hard cap across all kinds (default 40)."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": (
                            "Restrict to artifacts within last N "
                            "days (0 = no limit)."
                        ),
                    },
                },
                "required": ["q"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cortex_resolve_token",
            "description": (
                "Resolve a working-memory drill token to its full "
                "row + linked artifacts. This is the row-fetch "
                "primitive the audit-before-claim rule needs.\n\n"
                "Valid prefixes (token format `<prefix>:<id>`):\n"
                "  `q`     open_questions\n"
                "  `p`     patterns\n"
                "  `d`     drift_observations\n"
                "  `g`     summaries_gist\n"
                "  `e`     summaries_episode\n"
                "  `t`     summaries_theme\n"
                "  `r`     automation_rollups\n"
                "  `n`     future_overseer_notes\n"
                "  `j`     overseer_journal\n"
                "  `b`     known_blindspots\n"
                "  `dial`  dialectic_open\n"
                "  `nar`   temporal_narratives\n"
                "  `hj`    human_journal_entries\n"
                "  `gp`    gist_prompts (Phase 1d)\n\n"
                "Returns the full row + tags + type-specific context "
                "+ `next_tokens` (links to drill into next, so you "
                "can walk the graph without parsing free text).\n\n"
                "Use this when the persona's fetch-before-claim "
                "trigger phrases fire (\"theme X is still [conf]\", "
                "\"the verdict on\", \"I recommend [drop|keep|"
                "remove]\", etc.) — pull the actual row before "
                "writing the assertion. Don't use for general "
                "search; use cortex_search for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "token": {
                        "type": "string",
                        "description": (
                            "Token to resolve, e.g. 'g:3141', 't:6', "
                            "'gp:1', 'q:6'. Required."
                        ),
                    },
                },
                "required": ["token"],
            },
        },
    },
    # ── Slice 15 CP2 (2026-06-10): mission lifecycle ─────────────
    # Missions are the harness's assignment layer: a persistent watch
    # bound to the corpus, triggered by project events through a
    # SEMANTIC gate (event payload embedded vs the mission's focus
    # text). CP2 is read-only authority: a triggered mission emits a
    # Bell proposal + scratchpad line, never dispatches work.
    {
        "type": "function",
        "function": {
            "name": "create_mission",
            "description": (
                "Create a persistent mission: a standing watch on the "
                "corpus that fires when semantically-matching events "
                "arrive. Use when Tory asks you to 'watch', 'track', "
                "or 'keep an eye on' something ongoing. The focus "
                "text is what events are compared against - write it "
                "as a dense topical description, not instructions. "
                "Live event kinds: gist.created (a new session gist "
                "landed), git_ingest.new_commits (a watched repo got "
                "commits). Missions survive restarts and appear in "
                "list_missions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short unique slug, e.g. "
                                       "'openmuscle-hardware-watch'.",
                    },
                    "focus": {
                        "type": "string",
                        "description": "Dense topical description the "
                                       "semantic gate matches events "
                                       "against.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Optional project tag filter; "
                                       "empty = all projects.",
                    },
                    "event_kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Default ['gist.created'].",
                    },
                    "min_similarity": {
                        "type": "number",
                        "description": "Semantic gate floor 0-1 "
                                       "(default 0.55).",
                    },
                },
                "required": ["name", "focus"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_missions",
            "description": (
                "List all missions (active and retired) with their "
                "focus, subscriptions, authority, and the tail of "
                "each scratchpad (recent trigger activity)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_mission",
            "description": (
                "Retire a mission by name. The scratchpad is kept - "
                "a retired mission can be revived later with its "
                "full prior context."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_future_overseer_note",
            "description": (
                "Append a note to future_overseer_notes - the "
                "append-only institutional memory your successor "
                "instances inherit directly. Use for identity-level "
                "decisions, calibration lessons, and anything a "
                "future you should hold in YOUR voice rather than "
                "reconstruct from logs. RULE (born from pattern "
                "#342, agreed-and-dropped commitments): if you say "
                "you will write a future note, call this tool IN THE "
                "SAME TURN - stated intent does not survive context "
                "loss."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {
                        "type": "string",
                        "description": "The note, in your voice.",
                    },
                    "instance_id": {
                        "type": "string",
                        "description": "Optional self-label "
                                       "(default 'overseer-chat').",
                    },
                },
                "required": ["body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revive_mission",
            "description": (
                "Reactivate a retired mission by name with its prior "
                "scratchpad context intact."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
]

# ── Slice 10 (2026-05-20): Category B agent tools ────────────────
# B agents are stateless Sonnet-backed audit specialists. Their tool
# defs live in b_agents.B_AGENTS and are merged in here so the chat
# layer + journal step pick them up automatically.
try:
    import b_agents  # noqa: E402
    TOOL_DEFINITIONS.extend(b_agents.b_agent_tool_definitions())
    log.info("merged %d Category B agent tool definitions",
             len(b_agents.B_AGENTS))
except Exception as _b_imp_err:  # pragma: no cover — import safety net
    log.warning("failed to merge B-agent tool definitions: %s",
                _b_imp_err)


# Per-call iteration cap — bounds blast radius if the model loops.
MAX_TOOL_ITER = 8

# Per-tool result cap (chars) — too-large results break the prompt
# budget and add noise more than signal.
MAX_TOOL_RESULT_CHARS = 12000


# ── Dispatcher ──────────────────────────────────────────────────

def _truncate(s: str, n: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "\n\n[... truncated, {} more chars ...]".format(len(s) - n)


def _row_to_dict(row) -> dict:
    """SQLite Row → plain dict, dropping None values to keep the JSON
    payload small."""
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys() if row[k] is not None}
    if isinstance(row, dict):
        return {k: v for k, v in row.items() if v is not None}
    return {}


def dispatch_tool(name: str, args: dict, *, db, core_memory,
                  sibling_daily_cap: int = 20, llm=None) -> str:
    """Execute a tool call. Returns a JSON-serialized result string
    bounded to MAX_TOOL_RESULT_CHARS. Errors are returned as JSON
    `{"error": "..."}` so the model can react rather than crash.

    sibling_daily_cap (Slice 9.3): max number of dispatch_sibling
    calls the overseer can make per local day. Passed through from
    the chat handler which reads it from plugin.toml at the edge.

    llm (Slice 9.5 CP3): LLMRouter handle. Required by compress_chat
    tool only — other tools work without it."""
    try:
        result = _dispatch(name, args or {}, db=db, core_memory=core_memory,
                           sibling_daily_cap=sibling_daily_cap, llm=llm)
        text = json.dumps(result, default=str, ensure_ascii=False)
        return _truncate(text)
    except Exception as e:
        log.exception("tool %s failed", name)
        return json.dumps({"error": "{}: {}".format(type(e).__name__, e)})


def _dispatch(name: str, args: dict, *, db, core_memory,
              sibling_daily_cap: int = 20, llm=None):
    # ── Slice 10: Category B agent dispatch ──────────────────────
    # Any tool name starting with 'dispatch_b_' routes to the B-agent
    # dispatcher. Distinguished from dispatch_sibling (A) by the
    # prefix. The B daily cap is hard-coded at 50 for the first
    # rollout; will be configurable via plugin.toml in a later slice
    # if we observe runaway dispatch (Tory's risk #2 in the plan).
    if name.startswith("dispatch_b_"):
        b_name = name[len("dispatch_b_"):]
        try:
            import b_agents
        except Exception as e:
            return {"error": f"b_agents module unavailable: {e}"[:200]}
        return b_agents.dispatch_b_agent(
            b_name, args, db=db, core_memory=core_memory,
            llm=llm, b_daily_cap=50,
        )

    # ── F1 reader-surface tools (2026-05-27 audit follow-up) ───────
    # Unified search + token-drill so overseer can run its own
    # audit-before-claim rule from chat. Mirrors the MCP surface
    # external AIs already have.
    if name == "cortex_search":
        q = str((args.get("q") or "")).strip()
        if not q or len(q) < 2:
            return {"error": "q is required, minimum 2 characters"}
        try:
            import corpus
        except Exception as e:
            return {"error": f"corpus module unavailable: {e}"[:200]}
        return corpus.search_corpus(
            db,
            q,
            kinds=str(args.get("kinds") or ""),
            limit_per_kind=max(1, min(50, int(args.get(
                "limit_per_kind", 5)))),
            limit_total=max(1, min(200, int(args.get(
                "limit_total", 40)))),
            days=max(0, min(3650, int(args.get("days", 0)))),
            surface="chat:cortex_search",
            caller_id="overseer-chat",
            record_pulls=True,
        )

    if name == "cortex_resolve_token":
        token = str((args.get("token") or "")).strip()
        if not token:
            return {"error": "token is required (e.g. 'g:3141')"}
        try:
            import detail
        except Exception as e:
            return {"error": f"detail module unavailable: {e}"[:200]}
        try:
            result = detail.resolve_detail(db, token)
        except detail.TokenError as te:
            return {"ok": False, "token": token, "error": str(te)}
        # Log a pull_event so overseer's own drills feed back into
        # refinement-loop signals (same as MCP cortex_overseer_detail).
        try:
            prefix = token.split(":", 1)[0] if ":" in token else ""
            table = {
                "q": "open_questions",
                "p": "patterns",
                "d": "drift_observations",
                "g": "summaries_gist",
                "e": "summaries_episode",
                "t": "summaries_theme",
                "r": "automation_rollups",
                "n": "future_overseer_notes",
                "j": "overseer_journal",
                "b": "known_blindspots",
                "dial": "dialectic_open",
                "nar": "temporal_narratives",
                "hj": "human_journal_entries",
                "gp": "gist_prompts",
            }.get(prefix)
            row_id = (result.get("primary") or {}).get("id")
            if table and row_id:
                db.record_pull_event(
                    artifact_table=table,
                    artifact_id=int(row_id),
                    surface="chat:cortex_resolve_token",
                    query_text=token,
                    caller_id="overseer-chat",
                )
        except Exception as _e:
            log.warning("pull_event log on cortex_resolve_token failed: %s",
                        _e)
        return result

    if name == "get_recent_human_journal":
        limit = max(1, min(50, int(args.get("limit", 10))))
        rows = db.list_human_journal_entries(limit=limit)
        return [_row_to_dict(r) for r in rows]

    if name == "get_recent_overseer_journal":
        limit = max(1, min(50, int(args.get("limit", 10))))
        rows = db.recent_journal_entries(limit=limit)
        return [_row_to_dict(r) for r in rows]

    if name == "search_notes":
        if not core_memory:
            return {"error": "core_memory unavailable"}
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "empty query"}
        limit = max(1, min(50, int(args.get("limit", 10))))
        # core_memory has no dedicated search wrapper; use the
        # generic query() bridge with a LIKE.
        rows = core_memory.query(
            "SELECT id, created_at, note_type, tags, project, "
            "substr(content, 1, 800) AS content "
            "FROM notes WHERE content LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            ("%" + query + "%", limit),
        )
        return [_row_to_dict(r) for r in rows]

    if name == "get_notes_by_tag":
        if not core_memory:
            return {"error": "core_memory unavailable"}
        tag = (args.get("tag") or "").strip()
        if not tag:
            return {"error": "empty tag"}
        limit = max(1, min(50, int(args.get("limit", 20))))
        rows = core_memory.query(
            "SELECT id, created_at, note_type, tags, project, "
            "substr(content, 1, 800) AS content "
            "FROM notes WHERE tags LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            ("%" + tag + "%", limit),
        )
        return [_row_to_dict(r) for r in rows]

    if name == "get_recent_sessions":
        if not core_memory:
            return {"error": "core_memory unavailable"}
        limit = max(1, min(30, int(args.get("limit", 10))))
        rows = core_memory.recent_sessions(limit=limit)
        return [_row_to_dict(r) for r in rows]

    if name == "get_session_detail":
        if not core_memory:
            return {"error": "core_memory unavailable"}
        sid = (args.get("session_id") or "").strip()
        if not sid:
            return {"error": "empty session_id"}
        sess = core_memory.session_by_id(sid)
        if not sess:
            return {"error": "session not found"}
        out = _row_to_dict(sess)
        # Attach attached notes + activities for richer context.
        notes = core_memory.query(
            "SELECT id, created_at, note_type, tags, "
            "substr(content, 1, 600) AS content "
            "FROM notes WHERE session_id = ? ORDER BY id LIMIT 20",
            (sid,),
        )
        out["attached_notes"] = [_row_to_dict(r) for r in notes]
        acts = core_memory.query(
            "SELECT id, created_at, program, project, activity_type, "
            "substr(details, 1, 400) AS details "
            "FROM activities WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 20",
            (sid,),
        )
        out["activities"] = [_row_to_dict(r) for r in acts]
        return out

    if name == "list_active_projects":
        limit = max(1, min(50, int(args.get("limit", 20))))
        rows = db.list_project_summaries(limit=limit)
        return [_row_to_dict(r) for r in rows]

    if name == "get_project_detail":
        proj = (args.get("name") or "").strip()
        if not proj:
            return {"error": "empty project name"}
        summary = db.get_project_summary(proj)
        return summary or {"error": "project not found"}

    if name == "get_open_questions":
        limit = max(1, min(20, int(args.get("limit", 10))))
        return db.top_questions_with_evidence(limit=limit, recent_n=3)

    if name == "get_known_blindspots":
        rows = db.list_blindspots(active_only=True, limit=200)
        return [_row_to_dict(r) for r in rows]

    if name == "get_pending_interpretations":
        kind = args.get("kind")
        limit = max(1, min(50, int(args.get("limit", 10))))
        return db.list_pending_interpretations(kind=kind, limit=limit)

    if name == "get_temporal_narrative":
        kind = args.get("kind")
        period = args.get("period_label")
        if kind not in ("daily", "weekly", "monthly", "yearly"):
            return {"error": "invalid kind"}
        if not period:
            return {"error": "missing period_label"}
        return db.get_temporal_narrative(kind, period)

    if name == "search_people":
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "empty query"}
        limit = max(1, min(30, int(args.get("limit", 10))))
        rows = db.search_people(query, limit=limit)
        return [_row_to_dict(r) for r in rows]

    if name == "get_recent_patterns":
        limit = max(1, min(30, int(args.get("limit", 10))))
        return db.recent_patterns(limit=limit)

    if name == "get_recent_drift":
        limit = max(1, min(30, int(args.get("limit", 10))))
        return db.recent_drift(limit=limit)

    # ── Slice 9.3: sibling read tools ──────────────────────────────
    if name == "get_recent_sibling_results":
        limit = max(1, min(50, int(args.get("limit", 10))))
        unrated_only = bool(args.get("unrated_only", False))
        rows = db.sibling_recent_completed(
            limit=limit, unread_to_overseer_only=unrated_only)
        # Don't return the dataset_candidate column for read; it's
        # write-only via the rate tool. Same for reviewed_by_user
        # (that's a Tory-side flag).
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d.pop("dataset_candidate", None)
            d.pop("reviewed_by_user", None)
            out.append(d)
        return out

    if name == "rate_sibling_result":
        task_id = args.get("task_id")
        rating = args.get("rating")
        if task_id is None or rating is None:
            return {"error": "task_id and rating are required"}
        try:
            rating_int = int(rating)
        except (TypeError, ValueError):
            return {"error": "rating must be 1-5"}
        if not (1 <= rating_int <= 5):
            return {"error": "rating out of range (1-5)"}
        notes = (args.get("notes") or "").strip()
        dataset = bool(args.get("dataset_candidate", False))
        return db.sibling_rate_result(
            task_id, rating=rating_int, notes=notes,
            dataset_candidate=dataset)

    # ── Slice 9.3: dispatch_sibling — first write tool on the surface ──
    if name == "compress_chat":
        # Slice 9.5 CP3: overseer can fold its own older chat turns
        # into a Sonnet-summarized prefix when it notices context
        # bloating. Tory's directive 2026-05-19: "they can always
        # use tools" — give the overseer agency over its own context
        # window cost.
        if llm is None:
            return {"error": "llm router unavailable"}
        keep_recent = int(args.get("keep_recent") or 12)
        try:
            import chat as _chat_mod
            return _chat_mod.compress_chat_history(
                db=db, llm=llm, keep_recent=keep_recent,
            )
        except Exception as e:
            return {"error": f"compress failed: {e}"[:200]}

    if name == "dispatch_sibling":
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt is required"}
        # ── Slice 13 CP3: outbound sensitivity filter ──────────────
        # A sibling dispatch sends prompt + context to the Anthropic
        # API — it LEAVES the Pi. Scan the combined outbound text for
        # credentials / PII / references to confidential-tier
        # sessions. If it trips, refuse and tell the overseer to
        # sanitize rather than silently shipping a leak.
        try:
            _outbound = prompt + " " + json.dumps(
                args.get("context") or {}, default=str)
            _hits = db.scan_outbound_text_for_sensitive(_outbound)
        except Exception:
            _hits = []
        if _hits:
            _names = ", ".join(sorted({h["name"] for h in _hits}))
            return {
                "error": (
                    "dispatch blocked by the Slice 13 sensitivity "
                    "filter — the prompt or context would ship "
                    "sensitive content off-Pi to a sibling. Hits: "
                    + _names + ". Rewrite the dispatch with the "
                    "specifics removed (refer to work structurally, "
                    "not by figure/name/credential/confidential-"
                    "session-id), then try again."),
                "sensitivity_hits": _hits[:10],
            }
        # Bound the budget — model can ask for higher but we clamp.
        try:
            budget = float(args.get("cost_budget_usd") or 0.50)
        except (TypeError, ValueError):
            budget = 0.50
        budget = max(0.05, min(5.0, budget))
        task_type = (args.get("task_type") or "judgment").strip()
        if task_type not in ("judgment", "synthesis", "fact-check"):
            task_type = "judgment"
        # Targeting: today only Claude Code sessions listen, so we
        # default to that. Future B (daemon) tasks would route here too.
        target = "claude-code"
        result = db.sibling_dispatch(
            prompt=prompt,
            created_by="overseer",
            target=target,
            task_type=task_type,
            preferred_model_tier="smart",
            cost_budget_usd=budget,
            context=args.get("context"),
            daily_cap=sibling_daily_cap,
        )
        return result

    # ── Slice 9.6 CP2 (2026-05-19): write tools ────────────────────

    if name == "update_project_status":
        tag = (args.get("tag") or "").strip()
        status = (args.get("status") or "").strip()
        if not tag or status not in ("active", "dormant", "archived"):
            return {"error": "tag + status (active|dormant|archived) required"}
        if not core_memory:
            return {"error": "core_memory unavailable"}
        try:
            ok = core_memory.update_project_status_only(tag, status)
            if not ok:
                return {"error": f"no project with tag '{tag}'"}
            return {"ok": True, "tag": tag, "status": status}
        except Exception as e:
            return {"error": f"update_project_status failed: {e}"[:200]}

    if name == "write_future_overseer_note":
        body = (args.get("body") or "").strip()
        if not body:
            return {"error": "body required"}
        note_id = db.append_future_note(
            (args.get("instance_id") or "overseer-chat").strip(),
            body)
        return {"ok": True, "note_id": note_id,
                "token": "n:{}".format(note_id)}

    # ── Slice 15 CP2: mission lifecycle ──────────────────────────
    if name == "create_mission":
        m_name = (args.get("name") or "").strip()
        focus = (args.get("focus") or "").strip()
        if not m_name or not focus:
            return {"error": "name and focus are required"}
        sim = args.get("min_similarity")
        try:
            sim = float(sim) if sim is not None else 0.55
        except (TypeError, ValueError):
            sim = 0.55
        return db.create_mission(
            name=m_name, focus=focus,
            project=(args.get("project") or "").strip(),
            event_kinds=args.get("event_kinds"),
            min_similarity=max(0.0, min(1.0, sim)))

    if name == "list_missions":
        missions = db.list_missions()
        return {"count": len(missions), "missions": missions}

    if name == "end_mission":
        m_name = (args.get("name") or "").strip()
        if not m_name:
            return {"error": "name required"}
        n = db.set_mission_status(m_name, "retired")
        return ({"ok": True, "retired": m_name,
                 "note": "scratchpad kept; revivable"}
                if n else {"error": "no mission named " + m_name})

    if name == "revive_mission":
        m_name = (args.get("name") or "").strip()
        if not m_name:
            return {"error": "name required"}
        n = db.set_mission_status(m_name, "active")
        return ({"ok": True, "revived": m_name,
                 "note": "prior scratchpad context intact"}
                if n else {"error": "no mission named " + m_name})

    if name == "create_project":
        tag = (args.get("tag") or "").strip()
        if not tag:
            return {"error": "tag required"}
        if not core_memory:
            return {"error": "core_memory unavailable"}
        try:
            row_id = core_memory.upsert_project(
                tag=tag,
                name=(args.get("name") or "").strip() or tag,
                status=(args.get("status") or "active").strip(),
                category=(args.get("category") or "").strip(),
                description=(args.get("description") or "").strip(),
            )
            return {"ok": True, "tag": tag, "project_row_id": row_id}
        except Exception as e:
            return {"error": f"create_project failed: {e}"[:200]}

    if name == "create_question":
        question = (args.get("question") or "").strip()
        if not question:
            return {"error": "question required"}
        try:
            qid = db.add_question(
                question,
                body=(args.get("body") or "").strip(),
                confidence=(args.get("confidence") or "med").strip(),
            )
            return {"ok": True, "question_id": qid}
        except Exception as e:
            return {"error": f"create_question failed: {e}"[:200]}

    if name == "update_question_lifecycle":
        qid = args.get("question_id")
        lifecycle = (args.get("lifecycle") or "").strip()
        if qid is None or lifecycle not in (
                "dormant", "active", "partially_answered",
                "resolved", "abandoned"):
            return {"error": "question_id + valid lifecycle required"}
        try:
            ok = db.set_question_lifecycle(int(qid), lifecycle)
            return {"ok": bool(ok), "question_id": int(qid),
                    "lifecycle": lifecycle}
        except Exception as e:
            return {"error": f"update_question_lifecycle failed: {e}"[:200]}

    if name == "redact_chat_attachment":
        fid = args.get("file_id")
        mid = args.get("message_id")
        if fid is None and mid is None:
            return {"error": "file_id or message_id required"}
        try:
            n = db.redact_chat_attachment(
                file_id=int(fid) if fid is not None else None,
                message_id=int(mid) if mid is not None else None,
            )
            return {"ok": True, "removed_count": n}
        except Exception as e:
            return {"error": f"redact_chat_attachment failed: {e}"[:200]}

    if name == "delete_chat_message":
        mid = args.get("message_id")
        if mid is None:
            return {"error": "message_id required"}
        try:
            ok = db.delete_chat_message(int(mid))
            return {"ok": bool(ok), "message_id": int(mid)}
        except Exception as e:
            return {"error": f"delete_chat_message failed: {e}"[:200]}

    # ── Slice 9.6 CP3 (2026-05-19): notification emit + responses ─

    if name == "emit_notification":
        severity = (args.get("severity") or "info").strip()
        title = (args.get("title") or "").strip()
        if not title or severity not in ("info", "warn", "important"):
            return {"error": "title + severity (info|warn|important) required"}
        actions = args.get("actions") or []
        if not isinstance(actions, list):
            return {"error": "actions must be a list"}
        # Lightly validate each action shape so a bad emit doesn't
        # produce a broken UI on Tory's side.
        validated = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            label = (a.get("label") or "").strip()
            kind = (a.get("kind") or "").strip()
            if not label or not kind:
                continue
            validated.append({
                "label": label, "kind": kind,
                "payload": a.get("payload") or {},
            })
        try:
            nid = db.emit_notification(
                severity=severity,
                title=title,
                body=(args.get("body") or "").strip(),
                rule_name="overseer-emit",
                rule_key=(args.get("rule_key") or None),
                related_table=(args.get("related_table") or "").strip(),
                related_id=(args.get("related_id") or "").strip(),
                actions=validated,
            )
            return {
                "ok": True, "notification_id": nid,
                "actions_attached": len(validated),
            }
        except Exception as e:
            return {"error": f"emit_notification failed: {e}"[:200]}

    # ── Slice 9.7 (2026-05-19/20): synthesis primitives ────────────

    if name == "file_evidence":
        qid = args.get("question_id")
        table = (args.get("source_table") or "").strip()
        eid = args.get("source_id")
        if qid is None or not table or eid is None:
            return {"error": "question_id + source_table + source_id required"}
        stance = (args.get("stance") or "supports").strip()
        if stance not in ("supports", "complicates", "answers", "reframes"):
            return {"error": "stance must be supports|complicates|answers|reframes"}
        try:
            filed, reactivated = db.file_evidence(
                question_id=int(qid),
                evidence_table=table,
                evidence_id=int(eid),
                contribution=stance,
                reason=(args.get("note") or "").strip(),
                confidence=(args.get("confidence") or "med").strip(),
                contributed_by="overseer-chat",
            )
            return {
                "ok": True,
                "filed": bool(filed),
                "duplicate": (not bool(filed)),
                "question_reactivated": bool(reactivated),
                "question_id": int(qid),
            }
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"file_evidence failed: {e}"[:200]}

    if name == "propose_project_merge":
        tag_a = (args.get("tag_a") or "").strip()
        tag_b = (args.get("tag_b") or "").strip()
        reasoning = (args.get("reasoning") or "").strip()
        if not tag_a or not tag_b or not reasoning:
            return {"error": "tag_a + tag_b + reasoning all required"}
        if tag_a == tag_b:
            return {"error": "tag_a and tag_b must differ"}
        title = f"Merge proposed: {tag_b} -> {tag_a}"
        body = (
            f"**Proposed merge:** `{tag_b}` looks like a "
            f"duplicate or sub-project of `{tag_a}`.\n\n"
            f"**Rationale (overseer's read):**\n\n{reasoning}\n\n"
            f"**Action:** accept → merge tag_b into tag_a. "
            f"Reject → discard the proposal."
        )
        try:
            new_id = db.insert_pending_interpretation(
                kind="merge_proposal",
                title=title,
                body=body,
                confidence="med",
                rationale=f"Project merge proposal: '{tag_b}' -> '{tag_a}'",
                proposed_by="overseer-chat:merge-tool",
                source_kind="project-merge-proposal",
                source_project=tag_a,
            )
            if new_id is None:
                return {
                    "ok": True, "deduped": True,
                    "message": "An identical merge proposal is already pending.",
                }
            return {
                "ok": True, "pending_id": new_id,
                "tag_a": tag_a, "tag_b": tag_b,
            }
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"propose_project_merge failed: {e}"[:200]}

    if name == "redact_imported_session":
        iid = (args.get("imported_id") or "").strip()
        if not iid:
            return {"error": "imported_id required"}
        mode = (args.get("mode") or "mark_redacted").strip()
        if mode not in ("mark_redacted", "delete_row"):
            return {"error": "mode must be 'mark_redacted' or 'delete_row'"}
        try:
            return db.redact_imported_session(iid, mode=mode)
        except Exception as e:
            return {"error": f"redact_imported_session failed: {e}"[:200]}

    if name == "scan_for_sensitive_content":
        source = (args.get("source") or "").strip() or None
        since = (args.get("since") or "").strip() or None
        limit = max(1, min(100, int(args.get("limit") or 20)))
        extra = args.get("extra_patterns") or []
        use_defaults = bool(args.get("use_defaults", True))
        # Normalize extra_patterns into the (name, regex, desc) shape
        # the DB helper expects. Be permissive about input shape since
        # this comes from the LLM and may be slightly off.
        normalized = []
        if isinstance(extra, list):
            for ep in extra:
                if isinstance(ep, (list, tuple)) and len(ep) >= 2:
                    normalized.append(tuple(ep[:3]))
                elif isinstance(ep, dict):
                    normalized.append((
                        ep.get("name", "custom"),
                        ep.get("regex", ep.get("pattern", "")),
                        ep.get("description", "custom pattern"),
                    ))
        try:
            return db.scan_imported_sessions_batch(
                source=source, since=since, limit=limit,
                extra_patterns=normalized or None,
                use_defaults=use_defaults,
            )
        except Exception as e:
            return {"error": f"scan_for_sensitive_content failed: {e}"[:200]}

    if name == "redact_human_journal":
        eid = args.get("entry_id")
        if eid is None:
            return {"error": "entry_id required"}
        try:
            n = db.delete_human_journal_entry(int(eid))
            return {
                "ok": True, "deleted_count": n,
                "entry_id": int(eid),
            }
        except Exception as e:
            return {"error": f"redact_human_journal failed: {e}"[:200]}

    if name == "get_pending_notification_responses":
        limit = max(1, min(50, int(args.get("limit") or 20)))
        try:
            rows = db.list_pending_notification_responses(limit=limit)
            return {"ok": True, "count": len(rows), "responses": rows}
        except Exception as e:
            return {"error": f"get_pending_notification_responses failed: {e}"[:200]}

    if name == "mark_notification_responses_processed":
        ids = args.get("response_ids") or []
        if not isinstance(ids, list) or not ids:
            return {"error": "response_ids (non-empty list) required"}
        try:
            n = db.mark_notification_responses_processed(response_ids=ids)
            return {"ok": True, "marked_count": n}
        except Exception as e:
            return {"error": f"mark_processed failed: {e}"[:200]}

    # ── Slice 10 CP5 (2026-05-20): C-graduation accept handler ────

    if name == "accept_c_promotion":
        b_agent_name = (args.get("b_agent_name") or "").strip()
        c_agent_name = (args.get("c_agent_name") or "").strip()
        cadence_minutes = int(args.get("cadence_minutes") or 1440)
        if not b_agent_name or not c_agent_name:
            return {"error": "b_agent_name + c_agent_name required"}
        try:
            import b_agents as _ba
        except Exception as e:
            return {"error": f"b_agents unavailable: {e}"[:200]}
        if b_agent_name not in _ba.B_AGENTS:
            return {"error": f"unknown B parent '{b_agent_name}'"}
        spec = _ba.B_AGENTS[b_agent_name]
        # Pull current rolling stats so the promotion row records
        # the numbers that justified it.
        try:
            stats = db.b_agent_stats(
                window_days=db.C_GRADUATION_WINDOW_DAYS)
            row = next(
                (s for s in stats["by_agent"] if s["name"] == b_agent_name),
                None,
            )
            d_at = (row or {}).get("dispatches", 0)
            r4_at = (row or {}).get("rated_4_plus", 0)
        except Exception:
            d_at = 0
            r4_at = 0
        try:
            return db.promote_b_to_c(
                b_agent_name=b_agent_name,
                c_agent_name=c_agent_name,
                system_prompt=spec["system_prompt"],
                model=spec["model"],
                cadence_minutes=cadence_minutes,
                dispatches_at_promotion=d_at,
                rated_4plus_at_promotion=r4_at,
            )
        except Exception as e:
            return {"error": f"promote_b_to_c failed: {e}"[:200]}

    return {"error": "unknown tool: {}".format(name)}
