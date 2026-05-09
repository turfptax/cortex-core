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


def dispatch_tool(name: str, args: dict, *, db, core_memory) -> str:
    """Execute a tool call. Returns a JSON-serialized result string
    bounded to MAX_TOOL_RESULT_CHARS. Errors are returned as JSON
    `{"error": "..."}` so the model can react rather than crash."""
    try:
        result = _dispatch(name, args or {}, db=db, core_memory=core_memory)
        text = json.dumps(result, default=str, ensure_ascii=False)
        return _truncate(text)
    except Exception as e:
        log.exception("tool %s failed", name)
        return json.dumps({"error": "{}: {}".format(type(e).__name__, e)})


def _dispatch(name: str, args: dict, *, db, core_memory):
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

    return {"error": "unknown tool: {}".format(name)}
