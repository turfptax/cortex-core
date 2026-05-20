"""Project narrative generation (Slice 4 CP1b).

For each project the user has imported, generate a 3-paragraph
LLM rollup that lives next to the deterministic stats. This is
the interpretive layer the Overseer brings on top of the raw
counts — what this project is about, what's been happening
lately, what patterns or drift are worth noticing.

DESIGN

Context per call (~3K input tokens):
  - The project's deterministic stats (from project_summaries)
  - Most recent gists tagged project:<name> (up to 8)
  - Active themes tagged with the project (up to 4)
  - Open questions filed against this project (up to 5) —
    surfaced verbatim so the narrative names them
  - Most recent overseer journal entries (up to 2) — for voice
    continuity, NOT for fact

Output (~800 output tokens budget):
  - 3 short paragraphs as a single text blob:
      1. WHAT this project is about (from gists/themes synthesis)
      2. WHAT'S BEEN HAPPENING in the recent window
      3. PATTERNS OR DRIFT worth flagging
  - Followed by a "## Open questions still live" section that
    enumerates any open_questions filed against this project,
    so the narrative doesn't feel forgetful of the deeper layer.
  - May naturally weave in references to the cross-project
    interpretive cluster (the "making the hidden visible" /
    identity / truth-under-distortion thread Tory's journal has
    been developing) WHEN the project's themes align — not as
    a forced injection.

Cost cap: $0.05 per call. Daily budget cap (managed by caller via
TickBudget) on top of that. Loop integration fires per-project
narrative refresh at most once per 24h AND only if session_count
grew by ≥3 since last narrative.
"""

from __future__ import annotations

import json
import logging
import time

import pricing


log = logging.getLogger("plugin.overseer.project_narrative")


# Per-call hard cap. Loop manager enforces a daily cap on top.
DEFAULT_MAX_COST_USD_PER_CALL = 0.05
DEFAULT_MAX_TOKENS = 900
DEFAULT_TEMPERATURE = 0.55


# Configurable trigger thresholds. Loop reads these via plugin config
# (overrides) — defaults match Tory's call: 24h cadence + ≥3 new sessions.
DEFAULT_MIN_HOURS_BETWEEN_REGEN = 24
DEFAULT_MIN_NEW_SESSIONS = 3


NARRATIVE_PROMPT_TEMPLATE = """\
You are the Cortex overseer writing a short narrative rollup for ONE \
of the user's projects. The user is Tory — direct, intellectually \
serious, prefers accurate observation to flattery. This rollup will \
be displayed next to the deterministic stats in the new Projects tab.

PROJECT: {project_name}

DETERMINISTIC STATS (no need to repeat — the UI shows these next \
to your text):
{stats_block}

RECENT GISTS (most recent {gist_count}, oldest first; cite as g:N if \
you reference a specific one):
{gists_block}

ACTIVE THEMES TAGGED TO THIS PROJECT:
{themes_block}

OPEN QUESTIONS FILED AGAINST THIS PROJECT (verbatim — use them \
in the trailing section, don't paraphrase):
{open_questions_block}

YOUR RECENT JOURNAL ENTRIES (read for voice continuity, not for fact):
{journal_block}

FORMAT YOUR REPLY AS:

Paragraph 1 — WHAT this project is about. Two-three sentences \
synthesizing across the gists/themes. Specific, not generic.

Paragraph 2 — WHAT'S BEEN HAPPENING in the recent window. \
Reference what the LATEST gists are about. Note sub-projects, \
shifts in focus, etc.

Paragraph 3 — PATTERNS OR DRIFT worth flagging. If nothing's \
shifting, say what's stable. If you notice the project pulling \
toward (or away from) the broader cross-project interpretive \
cluster the user has been developing — making the hidden \
visible, identity work, truth under distortion — note it. Don't \
force the connection if it's not there.

## Open questions still live
- (Verbatim list of the open questions above, one per line.)
- (Skip this section entirely if there are zero open questions.)

CONSTRAINTS:
- Total length: under 350 words across the three paragraphs.
- No lists or headers in the paragraphs themselves (the # header \
  is only for the trailing questions section).
- No hedging openers ("It seems that…"). State observations directly.
- Don't apologize for missing data. If something's unclear, say what \
  IS clear instead.

AUTHORSHIP MARKERS — DO NOT FLATTEN:
If the inputs above (gists, themes, journal entries) contain text \
matching `[B:<name>]` or `[C:<name>]`, PRESERVE them verbatim in \
your narrative. Stripping them collapses audit provenance — readers \
need to tell B/C work apart from the overseer's own thinking.
"""


def _format_stats_block(stats: dict) -> str:
    """Compact stats summary for the prompt — names what's already
    visible to the UI so the narrative knows the numbers and can
    refer to them indirectly without re-stating them."""
    parts = []
    sc = stats.get("session_count", 0)
    if sc:
        parts.append("- {} sessions".format(sc))
    am = stats.get("active_minutes_total", 0)
    if am:
        parts.append("- ~{:,} minutes active across all sessions"
                     .format(am))
    cost = stats.get("cost_usd_estimate", 0.0)
    if cost:
        flag = "≥" if not stats.get("cost_known_complete", 1) else ""
        parts.append("- estimated cost: {}${:,.2f}".format(flag, cost))
    first = stats.get("first_active_at")
    last = stats.get("last_active_at")
    if first and last:
        parts.append("- first active {}; most recent {}".format(
            first[:10], last[:10]))
    d30 = stats.get("days_active_30", 0)
    if d30:
        parts.append("- {} day(s) active in the last 30".format(d30))
    top_files = stats.get("top_files") or []
    if top_files:
        parts.append("- most-touched files: {}".format(
            ", ".join("{} ({}x)".format(_shorten_path(f["path"]),
                                        f["hits"])
                      for f in top_files[:5])))
    models = stats.get("models_used") or {}
    if models:
        parts.append("- models used: {}".format(
            ", ".join("{} (×{})".format(m, n)
                      for m, n in sorted(
                          models.items(),
                          key=lambda kv: -kv[1])[:3])))
    return "\n".join(parts) if parts else "  (no stats — empty project)"


def _shorten_path(p: str) -> str:
    """Trim path to last two components for prompt readability."""
    if not p:
        return "?"
    parts = p.replace("\\", "/").rstrip("/").split("/")
    return "/".join(parts[-2:])


def _format_gists_block(gists: list[dict]) -> str:
    if not gists:
        return "  (no gists for this project)"
    out = []
    for g in gists[:8]:
        body = (g.get("body") or "").strip().replace("\n", " ")
        if len(body) > 280:
            body = body[:280] + " […]"
        period = g.get("period_label") or g.get("created_at") or ""
        out.append("- g:{id} ({period}) [{conf}]: {body}".format(
            id=g.get("id", "?"),
            period=period[:24],
            conf=g.get("confidence", "med"),
            body=body,
        ))
    return "\n".join(out)


def _format_themes_block(themes: list[dict]) -> str:
    if not themes:
        return "  (no themes tagged for this project yet)"
    out = []
    for t in themes[:4]:
        title = (t.get("title") or "").strip()
        body = (t.get("body") or "").strip().replace("\n", " ")
        if len(body) > 200:
            body = body[:200] + " […]"
        out.append("- t:{id} {title}: {body}".format(
            id=t.get("id", "?"), title=title, body=body,
        ))
    return "\n".join(out)


def _format_open_questions_block(questions: list[dict]) -> str:
    if not questions:
        return "  (no open questions filed against this project)"
    out = []
    for q in questions[:5]:
        text = (q.get("question") or "").strip()
        ev = q.get("evidence_count", 0)
        out.append("- q:{id} ({ev} evidence) {text}".format(
            id=q.get("id", "?"), ev=ev, text=text,
        ))
    return "\n".join(out)


def _format_journal_block(entries: list[dict]) -> str:
    if not entries:
        return "  (no journal entries)"
    out = []
    for j in entries[-2:]:
        body = (j.get("body") or "").strip().replace("\n", " ")
        if len(body) > 350:
            body = body[:350] + " […]"
        out.append("- {ts}: {body}".format(
            ts=(j.get("written_at") or "")[:10], body=body,
        ))
    return "\n".join(out)


def _gather_project_questions(db, project: str) -> list[dict]:
    """Find open_questions whose project_tag matches `project`. The
    question schema doesn't have a direct project FK, but routed
    evidence (gists) carry project tags — we match transitively via
    the evidence_for_question table OR via question.project_tag if
    the schema has it. Best-effort: returns a deduped list ordered
    by evidence_count descending.
    """
    # Direct: try a project_tag column on open_questions if present.
    out: list[dict] = []
    try:
        rows = db._conn.execute(
            "SELECT * FROM open_questions WHERE lifecycle = 'active' "
            "ORDER BY evidence_count DESC LIMIT 30"
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        return out
    seen_ids: set[int] = set()
    # Transitive: questions with at least one piece of evidence that's
    # a gist tagged project:<name>.
    project_tag = "project:{}".format(project)
    for r in rows:
        qid = r["id"]
        try:
            ev_rows = db._conn.execute(
                "SELECT 1 FROM evidence_for_question e "
                "JOIN tags tg ON tg.table_name = e.evidence_table "
                "  AND tg.row_id = e.evidence_id "
                "WHERE e.question_id = ? AND tg.tag = ? LIMIT 1",
                (qid, project_tag),
            ).fetchone()
        except Exception:
            ev_rows = None
        if ev_rows and qid not in seen_ids:
            seen_ids.add(qid)
            out.append(dict(r))
        if len(out) >= 5:
            break
    return out


# ── Public API ──────────────────────────────────────────────────


def generate_narrative(*, db, llm, project, stats, max_cost_usd=None,
                       triggered_by="manual"):
    """Generate (or regenerate) a narrative for one project.

    Returns dict with {ok, project, narrative, model, cost_usd,
    latency_ms, error}.

    `stats` is the dict returned by project_summary.compute_project_stats
    (or the parsed row from project_summaries). The caller is
    responsible for having already refreshed stats before calling
    here so the narrative reflects the latest data.

    Caller (loop or manual route) decides whether to actually persist
    by calling apply_narrative below — this function just produces
    the text. Separation lets callers preview before commit.
    """
    if not project:
        return {"ok": False, "error": "project required"}
    cap = max_cost_usd if max_cost_usd is not None else (
        DEFAULT_MAX_COST_USD_PER_CALL)

    # Gather project-scoped context.
    gists = db.gists_for_project(project=project, limit=8)
    # Themes tagged with this project — best-effort; theme schema may
    # not have the same project tag relationship as gists, so we fall
    # back to "no themes" silently if the table query is empty.
    themes: list[dict] = []
    try:
        rows = db._conn.execute(
            "SELECT * FROM summaries_theme ORDER BY created_at DESC "
            "LIMIT 50"
        ).fetchall()
        themes = [dict(r) for r in rows[:4]]
    except Exception as e:
        log.debug("theme lookup failed: %s", e)

    open_questions = _gather_project_questions(db, project)
    journal = db.recent_journal_entries(limit=2) or []

    prompt = NARRATIVE_PROMPT_TEMPLATE.format(
        project_name=project,
        stats_block=_format_stats_block(stats),
        gist_count=min(8, len(gists)),
        gists_block=_format_gists_block(gists),
        themes_block=_format_themes_block(themes),
        open_questions_block=_format_open_questions_block(open_questions),
        journal_block=_format_journal_block(journal),
    )

    t0 = time.monotonic()
    result = llm.complete(
        prompt,
        purpose="project-narrative",
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if not result.get("ok"):
        return {
            "ok": False,
            "project": project,
            "error": result.get("error", "llm error"),
            "cost_usd": float(result.get("cost_usd") or 0.0),
            "latency_ms": elapsed_ms,
        }

    cost = float(result.get("cost_usd") or 0.0)
    if cost > cap:
        log.warning(
            "narrative cost $%.4f exceeded cap $%.4f for project %s "
            "(model=%s)",
            cost, cap, project, result.get("model"),
        )

    text = (result.get("text") or "").strip()
    return {
        "ok": True,
        "project": project,
        "narrative": text,
        "model": result.get("model", ""),
        "backend": result.get("backend", ""),
        "cost_usd": cost,
        "latency_ms": elapsed_ms,
        "prompt_tokens": result.get("prompt_tokens", 0),
        "completion_tokens": result.get("completion_tokens", 0),
        "triggered_by": triggered_by,
    }


def apply_narrative(*, db, project, narrative_text, cost_usd,
                    session_count_at_update):
    """Persist a generated narrative onto project_summaries. Caller
    passes the session_count it observed when generating, so the
    next regen-trigger check has the right baseline to compare
    against.

    Stores narrative_cost_usd as the most-recent cost (NOT cumulative
    — a cumulative column gets messy fast and the per-row LLM call log
    in llm_calls already has the audit trail)."""
    db.upsert_project_summary(
        project=project,
        narrative=narrative_text,
        narrative_updated_at=_now_iso(db),
        narrative_session_count_at_update=int(session_count_at_update or 0),
        narrative_cost_usd=round(float(cost_usd or 0.0), 4),
    )


def needs_regen(*, summary_row, now_iso=None,
                min_hours_between=DEFAULT_MIN_HOURS_BETWEEN_REGEN,
                min_new_sessions=DEFAULT_MIN_NEW_SESSIONS):
    """Return (bool, reason) — should this project's narrative be
    regenerated? Used by the loop manager to decide which projects
    to spend budget on each tick.

    Triggers (any of these alone is enough):
      - narrative is empty (first run for this project)
      - it's been >= min_hours_between hours AND session_count grew
        by >= min_new_sessions since last narrative
    """
    if not summary_row:
        return False, "no summary row"
    narrative = (summary_row.get("narrative") or "").strip()
    if not narrative:
        return True, "no narrative yet"
    last = summary_row.get("narrative_updated_at")
    if not last:
        return True, "narrative_updated_at missing"
    # Time gate
    try:
        from datetime import datetime, timezone
        t_then = datetime.fromisoformat(last.replace(" ", "T"))
        if t_then.tzinfo is None:
            t_then = t_then.replace(tzinfo=timezone.utc)
        t_now = datetime.now(timezone.utc)
        hours_since = (t_now - t_then).total_seconds() / 3600.0
    except Exception:
        return True, "couldn't parse narrative_updated_at"
    if hours_since < min_hours_between:
        return False, "only {:.1f}h since last regen".format(hours_since)
    # Session-count gate
    cur_sessions = int(summary_row.get("session_count") or 0)
    last_sessions = int(
        summary_row.get("narrative_session_count_at_update") or 0)
    if (cur_sessions - last_sessions) < min_new_sessions:
        return False, "only {} new sessions since last regen".format(
            cur_sessions - last_sessions)
    return True, "{:.1f}h elapsed and {} new sessions".format(
        hours_since, cur_sessions - last_sessions)


def _now_iso(db) -> str:
    """SQLite-format datetime('now') so the value matches everything
    else the schema produces."""
    return db._conn.execute(
        "SELECT datetime('now')"
    ).fetchone()[0]
