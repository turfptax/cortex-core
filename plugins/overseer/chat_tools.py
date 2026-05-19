"""Overseer chat tools — Slice 10.

Read-only tool surface the overseer can call during a chat turn to
fetch live data the static context block doesn't already include.

Why these and not more?
- Tier 1: read-only inspection (notes, sessions, journals, projects,
  body/call/activity surfaces). Can't damage anything; bounded
  cost per call (small DB queries, no LLM).
- No write tools yet. The overseer should not mark blindspots
  acknowledged, mutate dialectic state, or change classifications
  without going through the existing (auditable) review queues.

Each tool follows OpenAI's function-calling schema (which OpenRouter
normalizes to Anthropic's native tool_use format for Opus). The
dispatcher takes (name, args) -> str and returns a JSON-serialized
result the model can read on the next turn.

Cost shape: each tool call adds one LLM round-trip. The chat handler
caps tool iterations per chat turn (see MAX_TOOL_ITER) so a confused
model can't loop indefinitely and burn budget.
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
]

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

    return {"error": "unknown tool: {}".format(name)}
