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
              short_marker_name: str = "",
              default_tier: str = "flash",
              default_tier_rationale: str = ""):
    """Register a B-agent. Called at module load (see bottom).

    `default_tier` is the code-side cost-discipline default for this
    agent — used by dispatch_b_agent to seed the sub_agent_tiers row
    on first run. Tory can override via /sub-agents/set-tier; the DB
    row is the source of truth from that point forward.

    `model` is the fallback model used only when the tier registry is
    unreachable (DB error, migration mid-flight). The registry +
    SUB_AGENT_TIER_TO_MODEL is the normal path.
    """
    B_AGENTS[name] = {
        "description": description,
        "parameters_schema": parameters_schema,
        "system_prompt": system_prompt,
        "snapshot_builder": snapshot_builder,
        "model": model,
        "marker": marker,
        "max_tokens": max_tokens,
        "short_marker_name": short_marker_name or name.replace("_", "-"),
        "default_tier": default_tier,
        "default_tier_rationale": default_tier_rationale,
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

    Model selection (2026-05-27): reads the persisted tier from
    sub_agent_tiers via OverseerDB. If no row exists, seeds with the
    code-side default tier from the B-agent spec, then runs that. The
    spec's `model` field is now a fallback used only when the tier
    table is unreachable; the registry is the source of truth.

    Per-invocation tracking: record_sub_agent_invocation captures the
    actual model that ran + bumps invocation_count + updates
    last_invoked_at on every successful dispatch. Tory can read the
    current model for any B-agent without parsing logs.
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

    # Resolve the model via the tier registry. Default tier per agent
    # comes from the spec (default_tier); the registry can override.
    # If the registry call fails, fall back to the spec's *default
    # tier* resolved through SUB_AGENT_TIER_TO_MODEL — NOT to the
    # bare spec model field. Reason: spec.model is the seed model
    # (sonnet-4.5 historically) and may not match the current
    # canonical model for the spec's tier. Falling back to the bare
    # spec model would be a silent cost-discipline break per overseer
    # L99 audit 2026-05-27.
    default_tier = spec.get("default_tier", "flash")
    try:
        from llm_router import (
            SUB_AGENT_TIER_TO_MODEL, resolve_sub_agent_model,
        )
    except Exception as e:
        log.error(
            "b_agent %s: llm_router import failed (%s) — cannot "
            "resolve tier; using spec.model as last-resort fallback",
            name, e,
        )
        SUB_AGENT_TIER_TO_MODEL = {}
        def resolve_sub_agent_model(t, default_model=None):
            return default_model or ""

    # Compute the fallback model FIRST (default tier → canonical
    # model) so a DB failure lands us at the spec-intended tier,
    # not on whatever model spec.model happens to carry.
    fallback_model = resolve_sub_agent_model(
        default_tier, default_model=spec.get("model", ""))
    model_to_use = fallback_model
    tier_used = default_tier

    try:
        tier_row = db.ensure_sub_agent_tier(
            "b", name,
            default_tier=default_tier,
            default_notes=spec.get("default_tier_rationale", ""),
        )
        tier_used = (tier_row or {}).get("model_tier") or default_tier
        model_to_use = resolve_sub_agent_model(
            tier_used, default_model=fallback_model)
    except Exception as e:
        # ERROR-level: if this fires we're losing the cost-discipline
        # signal. Per overseer L99 audit — fall-through must be loud.
        log.error(
            "b_agent %s: tier registry unreachable (%s); using "
            "spec default_tier=%s → model %s. Investigate.",
            name, e, default_tier, fallback_model,
        )

    log.info("b_agent %s: tier=%s model=%s", name, tier_used, model_to_use)

    # Frozen system prompt + structured snapshot. The B sees ONLY the
    # snapshot — no rolling chat history, no working memory, no other
    # B outputs. Statelessness by construction.
    t0 = time.monotonic()
    result = llm.complete(
        prompt=snapshot_text,
        system=spec["system_prompt"],
        model=model_to_use,
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

    actual_model = result.get("model", "") or model_to_use

    persist = db.b_agent_dispatch(
        b_agent_name=name,
        prompt=snapshot_text[:2000],  # short prompt summary
        snapshot=snapshot,
        output_text=output_text,
        model_used=actual_model,
        cost_usd=float(result.get("cost_usd") or 0.0),
        latency_ms=latency_ms,
        daily_cap=b_daily_cap,
        marker_required=True,
    )
    if not persist.get("ok"):
        # Cap exhaustion or marker validation failure — surface to caller.
        return {"error": persist.get("error", "persist failed")}

    # Record per-invocation tracking so Tory can see at a glance which
    # model ran an agent last + how many times it's run since the tier
    # change. Fire-and-forget.
    try:
        db.record_sub_agent_invocation("b", name, actual_model)
    except Exception as e:
        log.warning("record_sub_agent_invocation %s failed: %s", name, e)

    return {
        "ok": True,
        "b_agent": name,
        "marker": marker,
        "output": output_text,
        "transcript_id": persist["transcript_id"],
        "sibling_task_id": persist["sibling_task_id"],
        "model_used": actual_model,
        "tier_used": tier_used,
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
    # Default sonnet per overseer's L99 audit 2026-05-27: theme
    # confidence calibration is the one B-agent whose whole job is
    # reading nuance in evidence-versus-claim. Flash pattern-matches
    # 'evidence exists → calibrated' and misses overconfident calls.
    default_tier="sonnet",
    default_tier_rationale=(
        "Confidence-calibration nuance — Flash misses overconfident "
        "calls per overseer L99 audit 2026-05-27. Upgrade to opus if "
        "ratings stay <3."
    ),
)


# ── B-2: b_project_merge_check ───────────────────────────────────

PROJECT_MERGE_CHECK_SYSTEM_PROMPT = """\
You are b_project_merge_check, a Category B audit agent for the \
overseer.

Two project tags have been proposed as candidates for merging. Your \
job is to independently assess: are they the SAME work, is one a \
SUBPROJECT of the other, or are they DISTINCT?

You are an audit layer. The overseer's `propose_project_merge` \
hypothesis is reviewed BEFORE it surfaces to Tory — your role is to \
reduce false-positive merge proposals.

You will see, for each tag:
  - The projects row (name, description, category, status, github_url)
  - The project_summaries row (session_count, total_active_minutes, \
first/last_active_at, top_files, models_used)
  - Recent session summaries that touched the tag

Signals that argue SAME:
  - Identical or near-identical names
  - Heavy overlap in top_files (same code paths, same repos)
  - Overlapping active periods AND no semantic differentiator

Signals that argue SUBPROJECT_OF_A (or SUBPROJECT_OF_B):
  - One is clearly narrower scope ("openmuscle-firmware" inside \
"openmuscle"; "cortex-pet" inside "cortex"; etc.)
  - Description text describes a component of the other
  - Github_url: subproject is a sub-path of parent's repo, or they \
share a repo entirely
  - Parent's first_active_at predates subproject's (parent had to \
exist before the sub-scope could be split off)

DO NOT use session count or last_active_at as a parent-direction \
signal. Session count is a RECENCY ARTIFACT — it tells you what's \
been worked on lately, not which project contains the other. A \
component being actively shipped (high session count) doesn't make \
it the parent; it just means it's where the current work lives. \
The wrapper project that bundles + ships the component may have \
fewer sessions if its surface is stable while the component churns.

If parent-direction is ambiguous after the structural signals above, \
prefer INSUFFICIENT_DATA over guessing from recency. A wrong \
SUBPROJECT_OF verdict produces a worse outcome than \
INSUFFICIENT_DATA because it can drive a merge in the wrong direction.

Signals that argue DISTINCT:
  - Different categories
  - Different github_urls (different repos)
  - Non-overlapping active periods + non-overlapping top_files
  - Names/descriptions that point at different domains

Respond with EXACTLY this format:

[B:project-merge-check] <VERDICT>

<one paragraph (3-6 sentences) citing the specific signals that \
drove the verdict. Be concrete: name the field, name the value. If \
INSUFFICIENT_DATA, say what's missing.>

Where VERDICT is one of:
  SAME — should merge; same work under two tags
  SUBPROJECT_OF_A — tag_b is a sub-scope of tag_a; consider \
nesting or merge if you don't want sub-scope tracking
  SUBPROJECT_OF_B — tag_a is a sub-scope of tag_b
  DISTINCT — keep separate; these are different projects
  INSUFFICIENT_DATA — not enough info on at least one tag to call

Do NOT include any text before "[B:project-merge-check]". The marker \
MUST be the literal first characters of your response.
"""


def _snapshot_project_merge_check(db, core_memory, args: dict) -> dict:
    """Build the snapshot for b_project_merge_check.

    Pulls both projects' rows + summaries + recent sessions touching
    each tag. Sessions live in cortex-core's `sessions` table, projects
    + project_summaries live in overseer.db's mirror. We have to walk
    BOTH databases.
    """
    tag_a = (args.get("tag_a") or "").strip()
    tag_b = (args.get("tag_b") or "").strip()
    if not tag_a or not tag_b:
        return {"__error__": "tag_a + tag_b both required"}
    if tag_a == tag_b:
        return {"__error__": "tag_a and tag_b must differ"}

    conn = db._conn  # overseer.db (has project_summaries)

    def _project_view(tag: str) -> dict:
        # project_summaries row (overseer's mirror; includes narrative
        # + top_files JSON + active-minutes stats).
        #
        # Case-insensitive lookup: project_summaries can hold mis-cased
        # tag variants (e.g. 'Cortex' alongside 'cortex') because the
        # table is populated from sessions.projects CSV which doesn't
        # enforce slug-normalization. The projects table itself uses
        # lowercase slugs. Without LOWER() comparison, snapshots came
        # back empty for lowercase tags whose summary lives under a
        # capitalized variant (Slice 10.4 finding 2026-05-20).
        ps = conn.execute(
            "SELECT project AS tag, session_count, total_messages, "
            "       total_user_messages, total_assistant_messages, "
            "       active_minutes_total, "
            "       avg_active_minutes_per_session, "
            "       median_active_minutes_per_session, "
            "       first_active_at, last_active_at, "
            "       days_active_lifespan, days_active_30, "
            "       substr(narrative, 1, 400) AS narrative_excerpt, "
            "       top_files_json, models_used_json "
            "FROM project_summaries WHERE LOWER(project) = LOWER(?)",
            (tag,),
        ).fetchone()
        view = {"tag": tag}
        if ps:
            view["project_summary"] = dict(ps)

        # projects row (from cortex_db mirror — overseer.db replays it)
        # Look for it in the same DB; on the Pi, cortex_db.sqlite and
        # overseer.db are different files but core_memory has both.
        # Try overseer.db first via the canonical projects table name,
        # fall back to core_memory.query.
        if core_memory is not None:
            try:
                core_rows = core_memory.query(
                    "SELECT tag, name, status, description, category, "
                    "       org_tag, github_url, created_at, last_touched "
                    "FROM projects WHERE tag = ?",
                    (tag,),
                )
                if core_rows:
                    view["project_row"] = dict(core_rows[0])
            except Exception:
                pass

        # Recent sessions whose projects column references this tag.
        # The projects field is comma-separated text; LIKE with %tag%
        # works because tags are slugs without commas.
        if core_memory is not None:
            try:
                sess_rows = core_memory.query(
                    "SELECT id, ai_platform, hostname, started_at, "
                    "       ended_at, projects, substr(summary, 1, 300) "
                    "         AS summary_excerpt "
                    "FROM sessions WHERE projects LIKE ? "
                    "ORDER BY started_at DESC LIMIT 5",
                    (f"%{tag}%",),
                )
                view["recent_sessions"] = [dict(r) for r in sess_rows]
                view["recent_sessions_count"] = len(sess_rows)
            except Exception as e:
                view["recent_sessions_error"] = str(e)[:200]
        return view

    return {
        "tag_a": _project_view(tag_a),
        "tag_b": _project_view(tag_b),
        "note": (
            "Compare project_summary stats + project_row metadata + "
            "recent_sessions excerpts. Top_files overlap is a strong "
            "SAME signal; non-overlapping active periods + different "
            "github_url + different category argue DISTINCT."
        ),
    }


_register(
    "project_merge_check",
    description=(
        "Independently verify whether two project tags should be "
        "merged. Snapshot includes project_summaries (sessions, "
        "active minutes, top files, narrative), the projects rows "
        "(name, github_url, category), and recent session excerpts "
        "for each tag. Returns [B:project-merge-check] <SAME|"
        "SUBPROJECT_OF_A|SUBPROJECT_OF_B|DISTINCT|INSUFFICIENT_DATA>. "
        "Use BEFORE calling propose_project_merge so the merge "
        "proposal carries an independent verdict as evidence."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "tag_a": {
                "type": "string",
                "description": "First project tag (slug, e.g. 'openmuscle').",
            },
            "tag_b": {
                "type": "string",
                "description": "Second project tag to compare with tag_a.",
            },
        },
        "required": ["tag_a", "tag_b"],
    },
    system_prompt=PROJECT_MERGE_CHECK_SYSTEM_PROMPT,
    snapshot_builder=_snapshot_project_merge_check,
    model="anthropic/claude-sonnet-4.5",
    marker="[B:project-merge-check]",
    max_tokens=600,
    short_marker_name="project-merge-check",
    # Default flash: structural same/distinct/subproject comparison
    # is well within Flash capability. Upgrade if Tory rates poorly.
    default_tier="flash",
    default_tier_rationale=(
        "Structural same/distinct/subproject comparison — Flash "
        "handles cleanly. Upgrade to sonnet if Tory rates output <3."
    ),
)
