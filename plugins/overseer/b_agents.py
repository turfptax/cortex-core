"""Overseer Category B agents — Slice 10.

B agents are stateless, snapshot-on-demand specialists. Each is a
frozen system prompt + a snapshot-builder function + a target model.
They run synchronously from inside the overseer's tool dispatcher
(chat OR journal step) and return a result the overseer reads on
the next iteration.

The architectural shape (per slice_10_b_c_build_plan.md):

  overseer -> tool call (b_<name>) -> dispatch_b_agent()
              -> snapshot_builder(db, args)
              -> llm.complete(system=B.system_prompt, prompt=snapshot)
              -> validate marker [B:<name>] present
              -> persist via db.b_agent_dispatch()
              -> return short result to overseer

Why not subprocesses or daemons?
  The actual need is "stateless callable backed by Sonnet with a
  frozen prompt template." That's a function, not a process.

Why call them "agents" then?
  Because they have authorship — the [B:<name>] syntactic marker is
  preserved through consolidation passes so that when overseer cites
  a B verdict weeks later in a journal entry, the reader (Tory or
  another agent) can tell B was the source, not overseer's own
  thinking. Without that boundary, the corpus would silently
  collapse B work into overseer authorship.

To add a new B:
  1. Add an entry to B_AGENTS below with system_prompt + snapshot
     builder + tool definition.
  2. The tool definition gets exposed via b_agent_tool_definitions()
     and merged into chat_tools.TOOL_DEFINITIONS at module load.
  3. dispatch_b_agent() handles it generically — no per-B wiring
     needed in chat_tools.dispatch_tool.

The first two Bs (theme_check, project_merge_check) are defined in
this file. Future Bs go here too unless they grow large enough to
warrant their own module.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

log = logging.getLogger("plugin.overseer.b_agents")


# ── Per-B-agent definitions ──────────────────────────────────────

# A B-agent definition is a dict with:
#   - description: one-line tool description shown to overseer
#   - parameters_schema: JSON schema for tool args
#   - system_prompt: frozen instructions for the B
#   - snapshot_builder(db, core_memory, args) -> dict | None
#     (returns None on validation failure — surface error to caller)
#   - model: OpenRouter model id (Sonnet default for B prose, Bloom
#     for structured outputs)
#   - marker: required marker string in output (e.g. '[B:theme-check]')
#   - max_tokens: per-call output cap

B_AGENTS: dict[str, dict[str, Any]] = {
    # Defined later in this file via _register() to keep the lambda /
    # builder bodies near their docs. See bottom of module.
}

# Tool-definition prefix — every B tool is exposed as
# 'dispatch_b_<name>' so the dispatcher can route generically.
B_TOOL_PREFIX = "dispatch_b_"


def _register(name: str, *, description: str, parameters_schema: dict,
              system_prompt: str, snapshot_builder: Callable,
              model: str, marker: str, max_tokens: int = 800,
              short_marker_name: str = ""):
    """Register a B-agent. Called at module load (see bottom)."""
    B_AGENTS[name] = {
        "description": description,
        "parameters_schema": parameters_schema,
        "system_prompt": system_prompt,
        "snapshot_builder": snapshot_builder,
        "model": model,
        "marker": marker,
        "max_tokens": max_tokens,
        "short_marker_name": short_marker_name or name.replace("_", "-"),
    }


def b_agent_tool_definitions() -> list[dict]:
    """Return OpenAI-function-calling tool definitions for every B
    agent. chat_tools.TOOL_DEFINITIONS merges these in at module
    import. Distinguished by the `dispatch_b_` prefix.
    """
    out = []
    for name, spec in B_AGENTS.items():
        out.append({
            "type": "function",
            "function": {
                "name": f"{B_TOOL_PREFIX}{name}",
                "description": spec["description"],
                "parameters": spec["parameters_schema"],
            },
        })
    return out


# ── Dispatcher ───────────────────────────────────────────────────

def dispatch_b_agent(name: str, args: dict, *, db, core_memory,
                     llm, b_daily_cap: int = 50) -> dict:
    """Run a B-agent synchronously and persist the audit row.

    Returns a dict the chat-tool layer will JSON-serialize as the
    tool result. Errors are surfaced as `{error: "..."}` so the
    overseer can react rather than crash.
    """
    if name not in B_AGENTS:
        return {"error": f"unknown B agent: {name}"}
    spec = B_AGENTS[name]
    if llm is None:
        return {"error": "llm router unavailable"}

    # Build the snapshot — this is where verdict-vs-calibration
    # discipline is structurally enforced (e.g. theme_check slices
    # evidence by contributed_at <= theme.created_at).
    try:
        snapshot = spec["snapshot_builder"](db, core_memory, args or {})
    except Exception as e:
        log.exception("b_agent %s snapshot_builder failed", name)
        return {"error": f"snapshot_builder failed: {e}"[:200]}
    if snapshot is None:
        return {"error": "snapshot builder returned no data — check args"}
    if isinstance(snapshot, dict) and snapshot.get("__error__"):
        return {"error": snapshot["__error__"]}

    snapshot_text = json.dumps(snapshot, default=str, ensure_ascii=False,
                               indent=2)
    log.info("b_agent %s: snapshot built (%d chars)", name,
             len(snapshot_text))

    # Frozen system prompt + structured snapshot. The B sees ONLY the
    # snapshot — no rolling chat history, no working memory, no other
    # B outputs. Statelessness by construction.
    t0 = time.monotonic()
    result = llm.complete(
        prompt=snapshot_text,
        system=spec["system_prompt"],
        model=spec["model"],
        max_tokens=spec["max_tokens"],
        temperature=0.3,  # B is an audit — low temp; not creative work
        purpose=f"b_agent:{name}",
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    if not result.get("ok"):
        return {
            "error": f"LLM call failed: {result.get('error', 'unknown')}"[:200],
        }

    output_text = (result.get("text") or "").strip()
    marker = spec["marker"]
    # Defensive marker enforcement — if the model dropped it, prepend.
    # We log this so we can detect prompt-language failure modes.
    if marker not in output_text:
        log.warning("b_agent %s: model dropped marker '%s'; wrapping",
                    name, marker)
        output_text = f"{marker} (auto-wrapped) {output_text}"

    persist = db.b_agent_dispatch(
        b_agent_name=name,
        prompt=snapshot_text[:2000],  # short prompt summary
        snapshot=snapshot,
        output_text=output_text,
        model_used=result.get("model", ""),
        cost_usd=float(result.get("cost_usd") or 0.0),
        latency_ms=latency_ms,
        daily_cap=b_daily_cap,
        marker_required=True,
    )
    if not persist.get("ok"):
        # Cap exhaustion or marker validation failure — surface to caller.
        return {"error": persist.get("error", "persist failed")}

    return {
        "ok": True,
        "b_agent": name,
        "marker": marker,
        "output": output_text,
        "transcript_id": persist["transcript_id"],
        "sibling_task_id": persist["sibling_task_id"],
        "model_used": result.get("model", ""),
        "cost_usd": float(result.get("cost_usd") or 0.0),
        "latency_ms": latency_ms,
        "used_today": persist.get("used_today"),
        "cap": persist.get("cap"),
    }


# ── B-1: b_theme_check (calibration audit) ───────────────────────

THEME_CHECK_SYSTEM_PROMPT = """\
You are b_theme_check, a Category B audit agent for the overseer.

You are auditing whether an overseer confidence tag on a THEME was \
CALIBRATED at the time it was written. You are NOT evaluating \
whether the theme is correct in retrospect.

Two separate things you MUST keep apart:
1. Was the theme TRUE? (NOT your concern — out of scope.)
2. Did the evidence AVAILABLE AT WRITE-TIME support the chosen \
confidence tag? (The ONLY question.)

Read the theme's title/body + the evidence rows. The snapshot has \
ALREADY been sliced to only include evidence whose contributed_at \
is on or before the theme's created_at. Anything that happened after \
is invisible to you BY DESIGN — that's the structural defense \
against verdict-creep.

If you find yourself reasoning "and the subsequent evidence shows..." \
STOP — that's verdict-creep, not calibration. If the snapshot omits \
later evidence (it does), don't speculate about it.

Respond with EXACTLY this format:

[B:theme-check] <VERDICT>

<one paragraph (3-6 sentences) explaining the gap (if any) between \
confidence-at-write-time and evidence-at-write-time. Cite specific \
evidence rows by their id if relevant. If verdict is INSUFFICIENT_\
EVIDENCE_TO_JUDGE_CALIBRATION, say what would have been needed.>

Where VERDICT is one of:
  CALIBRATED — evidence-at-write-time clearly supported the tag
  OVERCONFIDENT — evidence-at-write-time was too thin for the tag
  UNDERCONFIDENT — evidence-at-write-time supported a stronger tag
  INSUFFICIENT_EVIDENCE_TO_JUDGE_CALIBRATION — too little to call

Do NOT include any text before "[B:theme-check]". The marker MUST \
be the literal first characters of your response.
"""


def _snapshot_theme_check(db, core_memory, args: dict) -> dict:
    """Build the snapshot for b_theme_check.

    CRITICAL: slices evidence_for_question rows by
    contributed_at <= theme.created_at. This is the structural defense
    against verdict-creep — the agent literally doesn't see evidence
    that came after the theme was tagged.
    """
    theme_id = args.get("theme_id")
    if theme_id is None:
        return {"__error__": "theme_id required"}
    try:
        theme_id = int(theme_id)
    except (TypeError, ValueError):
        return {"__error__": "theme_id must be int"}

    conn = db._conn
    theme = conn.execute(
        "SELECT id, title, body, confidence, first_seen_at, "
        "       last_reinforced_at, created_at "
        "FROM summaries_theme WHERE id = ?",
        (theme_id,),
    ).fetchone()
    if not theme:
        return {"__error__": f"no theme with id {theme_id}"}
    theme_d = dict(theme)
    theme_created_at = theme_d.get("created_at") or theme_d.get(
        "first_seen_at") or ""

    # Pull evidence rows that reference this theme as their evidence,
    # but ONLY rows whose contributed_at is <= theme.created_at.
    # Note: evidence_for_question maps a (question_id, evidence_table,
    # evidence_id) triple — themes appear here as evidence_table=
    # 'summaries_theme'. We surface the question links so the B can
    # see which open questions this theme is filed under.
    ev_rows = conn.execute(
        "SELECT e.id, e.question_id, e.contribution, e.reason, "
        "       e.confidence, e.contributed_at, e.contributed_by, "
        "       q.question, q.confidence AS question_confidence "
        "FROM evidence_for_question e "
        "LEFT JOIN open_questions q ON q.id = e.question_id "
        "WHERE e.evidence_table = 'summaries_theme' "
        "AND e.evidence_id = ? "
        "AND e.contributed_at <= ? "
        "ORDER BY e.contributed_at ASC",
        (theme_id, theme_created_at),
    ).fetchall()
    evidence = [dict(r) for r in ev_rows]

    # Count what was EXCLUDED by the timestamp slice. Transparent so
    # the B can note "5 later rows exist, not shown" in its reasoning
    # without seeing them.
    later_count = conn.execute(
        "SELECT COUNT(*) AS n FROM evidence_for_question "
        "WHERE evidence_table = 'summaries_theme' "
        "AND evidence_id = ? "
        "AND contributed_at > ?",
        (theme_id, theme_created_at),
    ).fetchone()["n"]

    return {
        "theme": theme_d,
        "evidence_at_or_before_theme_write_time": evidence,
        "evidence_count_visible_to_audit": len(evidence),
        "evidence_count_after_write_time_excluded": int(later_count or 0),
        "note": (
            "Evidence rows above are ALL the evidence that existed at "
            "or before the theme's created_at. Anything after was "
            "deliberately excluded from this snapshot."
        ),
    }


_register(
    "theme_check",
    description=(
        "Run a CALIBRATION audit on an overseer theme: was the "
        "confidence tag justified by evidence AVAILABLE AT WRITE-"
        "TIME? Not whether the theme is correct in retrospect. "
        "Returns [B:theme-check] <CALIBRATED|OVERCONFIDENT|"
        "UNDERCONFIDENT|INSUFFICIENT_EVIDENCE_TO_JUDGE_CALIBRATION> "
        "+ one paragraph. Snapshot slices evidence by "
        "contributed_at <= theme.created_at as a structural defense "
        "against verdict-creep."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "theme_id": {
                "type": "integer",
                "description": "ID of the summaries_theme row to audit.",
            },
        },
        "required": ["theme_id"],
    },
    system_prompt=THEME_CHECK_SYSTEM_PROMPT,
    snapshot_builder=_snapshot_theme_check,
    model="anthropic/claude-sonnet-4.5",
    marker="[B:theme-check]",
    max_tokens=600,
    short_marker_name="theme-check",
)
