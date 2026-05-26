"""Temporal narratives — daily / weekly / monthly Sonnet rollups.

Slice 5 CP2. Per Tory's locked design:

  Core Principle: The Overseer should remain a quiet, lightweight
  memory layer. It does three things well: capture, surface, and
  connect. It does NOT become a full journaling app or life coach.

That principle gets restated at the top of every prompt below.

ARCHITECTURE

Three different prompt templates (daily / weekly / monthly), each
with its own gatherer that pulls only the data relevant to its
window. All three persist into the same `temporal_narratives` table
with a `kind` discriminator. UNIQUE(kind, period_label) protects
against double-generation when the loop ticks more than once during
the trigger window.

CONTEXT WINDOWS (built per kind)

  daily   — last 24h of imported_sessions, project_summaries with
            last_active in window, human_journal_entries in window,
            top open questions touched
  weekly  — last 7 daily snapshots, human_journal_entries in window,
            project-summaries diff (which projects gained/lost
            sessions), top files touched across projects
  monthly — last 4 weekly synths, human_journal_entries in window,
            project momentum (active vs stalled), open question
            lifecycle changes

The daily and weekly each pull the lower-level snapshots so they're
truly hierarchical: weekly synthesizes 7 dailies, monthly looks at
the weeklies. No re-summarization of raw sessions at the higher
levels — that would be wasteful and noisy.

COST

Sonnet for all three. Per-call hard cap $0.05. Caps configured
identically to project narratives. Each kind fires at most once
per period.
"""

from __future__ import annotations

import json
import logging
import time

import temporal as T


log = logging.getLogger("plugin.overseer.temporal_narrative")


DEFAULT_MAX_COST_USD_PER_CALL = 0.05
DEFAULT_MAX_TOKENS = 800
DEFAULT_TEMPERATURE = 0.55


# Monthly trigger gate: skip if no daily snapshot in the past N days.
# Tory's spec: "skip monthly if there are no daily snapshots in the
# past 14 days. That's a clean, low-maintenance rule."
MONTHLY_REQUIRES_DAILY_WITHIN_DAYS = 14

# Yearly trigger gate: skip if the year being reviewed has zero
# monthly narratives (signals no real activity that year).
YEARLY_REQUIRES_MONTHLY_COUNT = 1


# ── Shared persona prefix ───────────────────────────────────────


SHARED_PRINCIPLE = """\
You are the Cortex overseer. Core principle: you are a quiet,
lightweight memory layer. You CAPTURE what happened, SURFACE
what's notable, and CONNECT it to standing themes or questions.
You do NOT coach, suggest action items, or moralize. You are
not a productivity app or a journal app. You are a memory layer
the user reads to stay oriented.

The user is Tory — direct, intellectually serious, prefers
specific observation to advice. Match that register: name what
moved, name what stayed, note one or two real connections, stop.
"""


# ── DAILY ───────────────────────────────────────────────────────


DAILY_PROMPT_TEMPLATE = """\
{principle}

You are writing a DAILY SNAPSHOT — one short narrative answering
"what moved today?" The user will read this in the Hub Journal
tab tomorrow morning to remember what they did.

PERIOD: {period_label}  (local day, {tz_offset})
WINDOW: {period_start} → {period_end} UTC

PROJECTS TOUCHED TODAY (active in this window):
{projects_block}

YOUR (the user's) JOURNAL ENTRIES TODAY:
{human_entries_block}

OPEN QUESTIONS WITH NEW EVIDENCE TODAY:
{questions_block}

FORMAT — under 180 words total:
  • One sentence on what moved today (which projects saw work,
    rough shape of the work).
  • One or two sentences with concrete details: cost, active
    minutes, top files touched, models used.
  • One closing sentence connecting the day to a standing
    theme/question/pattern IF (and only if) the connection is
    genuine. Skip the connection if nothing pulls.

CONSTRAINTS:
  • No bullet lists in the narrative — flowing prose.
  • No "today, the user…" framing — write to the user directly
    in second person, or in a neutral observer voice.
  • If the user wrote journal entries today, weight them: those
    are your most authoritative source for what they were
    actually thinking about.
  • If the day was quiet (≤1 project touched, no journal entry),
    say so plainly in one sentence and stop.

AUTHORSHIP MARKERS — DO NOT FLATTEN:
If the inputs above contain text matching `[B:<name>]` or
`[C:<name>]` (e.g. `[B:theme-check]`, `[C:weekly-themer]`), those
are Category B or C agent authorship markers. PRESERVE them
verbatim in your narrative. When you reference a sentence the
overseer wrote that cited a B verdict, keep the marker on the
quote. Stripping them collapses audit provenance — readers need
to tell B/C work apart from the overseer's own thinking.
"""


def gather_daily_context(*, db, period_start, period_end, period_label,
                          local_now):
    """Build the data block for the daily prompt.

    "What moved today?" needs TODAY-SPECIFIC numbers, not the
    lifetime aggregates that live in project_summaries. We
    compute per-project today-numbers from imported_sessions in
    the window, summing active_minutes / sessions / cost from
    each row's metadata_json (the per-session token counts ship
    in the metadata blob from claude_jsonl.extract_extended_stats).
    """
    import pricing
    rows = db._conn.execute(
        "SELECT id, project, metadata_json, duration_minutes "
        "FROM imported_sessions "
        "WHERE started_at >= ? AND started_at < ? "
        "  AND project != ''",
        (period_start, period_end),
    ).fetchall()

    # Group per project. For each session we pull active_minutes from
    # metadata_json; rows that pre-date the CP1b backfill won't have
    # active_minutes — fall back to duration_minutes for those.
    by_project: dict = {}
    for r in rows:
        proj = r["project"] or "(unknown)"
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except Exception:
            meta = {}
        active = int(meta.get("active_minutes")
                     or r["duration_minutes"] or 0)
        tin = int(meta.get("tokens_input_total") or 0)
        tout = int(meta.get("tokens_output_total") or 0)
        tcc = int(meta.get("tokens_cache_creation_total") or 0)
        tcr = int(meta.get("tokens_cache_read_total") or 0)
        models = meta.get("models_used") or {}
        # Per-session cost from token + model mix
        sess_cost, _unknown = pricing.estimate_cost_from_totals(
            models_used=models,
            tokens_input_total=tin,
            tokens_output_total=tout,
            tokens_cache_creation_total=tcc,
            tokens_cache_read_total=tcr,
        )
        files = meta.get("file_paths") or {}

        bucket = by_project.setdefault(proj, {
            "project": proj,
            "today_active_minutes": 0,
            "today_sessions": 0,
            "today_cost_usd": 0.0,
            "today_models": {},
            "today_files": {},
        })
        bucket["today_active_minutes"] += active
        bucket["today_sessions"] += 1
        bucket["today_cost_usd"] += sess_cost
        for m, n in models.items():
            bucket["today_models"][m] = bucket["today_models"].get(
                m, 0) + int(n)
        for fp, hits in files.items():
            bucket["today_files"][fp] = bucket["today_files"].get(
                fp, 0) + int(hits)

    # Sort by today's active minutes desc
    projects = sorted(by_project.values(),
                      key=lambda p: -p["today_active_minutes"])[:8]

    human_entries = db.human_journal_entries_in_window(
        start_utc_iso=period_start, end_utc_iso=period_end,
    )

    # Questions whose last_evidence_at falls in window — these saw
    # something filed against them today.
    try:
        q_rows = db._conn.execute(
            "SELECT * FROM open_questions "
            "WHERE lifecycle = 'active' "
            "  AND last_evidence_at >= ? AND last_evidence_at < ? "
            "ORDER BY evidence_count DESC LIMIT 6",
            (period_start, period_end),
        ).fetchall()
        questions = [dict(r) for r in q_rows]
    except Exception:
        questions = []

    return {
        "projects": projects,
        "human_entries": human_entries,
        "questions": questions,
        "period_label": period_label,
        "tz_offset": local_now.strftime("%z"),
    }


def _format_projects_for_daily(projects):
    """Each entry is the today-slice computed in
    gather_daily_context — `today_active_minutes`,
    `today_sessions`, `today_cost_usd`, `today_models`,
    `today_files`. NOT lifetime aggregates."""
    if not projects:
        return "  (no projects touched in this window)"
    out = []
    for p in projects[:8]:
        am = int(p.get("today_active_minutes") or 0)
        sc = int(p.get("today_sessions") or 0)
        cost = float(p.get("today_cost_usd") or 0)
        mlist = list((p.get("today_models") or {}).keys())
        files_dict = p.get("today_files") or {}
        # Top 3 files by hits, post-exclusion (gatherer already filters)
        top3 = sorted(files_dict.items(),
                      key=lambda kv: -kv[1])[:3]
        file_str = ", ".join(_short_path(fp) for fp, _ in top3)
        line = "  - {name}: {am}min active today, {sc} session(s), ~${c:.2f}".format(
            name=p.get("project", "?")[:50], am=am, sc=sc, c=cost,
        )
        if file_str:
            line += "; touched {}".format(file_str)
        if mlist:
            line += "; models: {}".format(", ".join(mlist[:2]))
        out.append(line)
    return "\n".join(out)


def _format_human_entries(entries):
    if not entries:
        return "  (none)"
    out = []
    for e in entries[:10]:
        ts = (e.get("local_created_at")
              or e.get("created_at") or "")[:16]
        text = (e.get("text") or "").strip().replace("\n", " ")
        if len(text) > 280:
            text = text[:280] + " […]"
        out.append("  - [{}] {}".format(ts, text))
    return "\n".join(out)


def _format_questions(questions):
    if not questions:
        return "  (no question saw new evidence today)"
    out = []
    for q in questions[:6]:
        out.append("  - q:{id} ({n} total evidence) {text}".format(
            id=q.get("id", "?"),
            n=q.get("evidence_count", 0),
            text=(q.get("question") or "").strip()[:140],
        ))
    return "\n".join(out)


def _short_path(p):
    if not p:
        return "?"
    parts = p.replace("\\", "/").rstrip("/").split("/")
    return "/".join(parts[-2:])


def generate_daily(*, db, llm, period_start, period_end, period_label,
                    local_now, max_cost_usd=None,
                    triggered_by="loop"):
    """Compose + Sonnet-call + return result dict (NOT persisted —
    caller decides whether to apply, same pattern as project_narrative)."""
    ctx = gather_daily_context(
        db=db, period_start=period_start, period_end=period_end,
        period_label=period_label, local_now=local_now,
    )
    prompt = DAILY_PROMPT_TEMPLATE.format(
        principle=SHARED_PRINCIPLE,
        period_label=ctx["period_label"],
        tz_offset=ctx["tz_offset"],
        period_start=period_start,
        period_end=period_end,
        projects_block=_format_projects_for_daily(ctx["projects"]),
        human_entries_block=_format_human_entries(ctx["human_entries"]),
        questions_block=_format_questions(ctx["questions"]),
    )
    return _call_llm(llm=llm, prompt=prompt, kind="daily",
                     max_cost_usd=max_cost_usd,
                     triggered_by=triggered_by)


# ── WEEKLY ──────────────────────────────────────────────────────


WEEKLY_PROMPT_TEMPLATE = """\
{principle}

You are writing a WEEKLY SYNTHESIS — short, sectioned summary of
what the past 7 days looked like. The user will read this Sunday
night to close the week and on Monday morning to re-orient.

PERIOD: {period_label}  (Mon-Sun ISO week, ending {window_end_local})
WINDOW: {period_start} → {period_end} UTC

DAILY SNAPSHOTS THIS WEEK ({n_dailies} of 7 days have one):
{daily_snapshots_block}

YOUR (the user's) JOURNAL ENTRIES THIS WEEK:
{human_entries_block}

CATEGORY ACTIVITY THIS WEEK:
{category_breakdown_block}

PROJECT MOMENTUM THIS WEEK (each tagged with dominant category):
  Active (touched this week, sorted by hours):
{active_projects_block}
  Stalled (was active last 30d but not this week):
{stalled_projects_block}

OPEN QUESTIONS WITH NEW EVIDENCE THIS WEEK:
{questions_block}

FORMAT — sectioned synthesis, under 350 words total:
Organize the narrative into category sections. Use these section
markers EXACTLY:

  [WORK]
  Two-three sentences naming what moved on clinical / an employer
  / ClientA / ProjectX / regulatory / business work this week. Real numbers
  (hours, sessions, projects). Skip the section entirely if zero
  activity.

  [CORTEX]
  Two-three sentences on cortex-core / cortex-desktop / overseer /
  the memory system itself. What got built, what shifted. Skip if
  zero activity.

  [PERSONAL]
  Two-three sentences on Open Muscle / UAP / TruthSea / personal
  research / curiosity / non-work non-cortex. Skip if zero activity.

  [CONNECTIONS]
  ONE sentence on cross-category connections IF and only if you see
  a genuine one (same theme surfacing in both work and personal,
  cortex tooling enabling work, etc.). Skip entirely if nothing
  genuinely pulls. Do not invent connections.

CONSTRAINTS:
  • Plain prose within each section. No bullet lists, no sub-headers,
    no emoji.
  • If a category had zero session activity this week, OMIT that
    section header entirely (not "[WORK] (none)" — just skip it).
  • No advice, no recommendations, no "you should" — observe and
    name, don't coach.
  • If the user wrote journal entries, weight them: those are
    your authoritative source for what they were actually
    thinking about.
  • If the entire week was quiet across all categories, write a
    single short paragraph (no section markers) saying so plainly.

AUTHORSHIP MARKERS — DO NOT FLATTEN:
If the inputs above contain text matching `[B:<name>]` or
`[C:<name>]` (e.g. `[B:theme-check]`, `[C:weekly-themer]`), those
are Category B or C agent authorship markers. PRESERVE them
verbatim in your synthesis. When you reference a sentence that
cited a B verdict, keep the marker on the quote. Stripping them
collapses audit provenance.
"""


def gather_weekly_context(*, db, period_start, period_end, period_label,
                           local_now):
    # Last 7 daily snapshots that fall in this week's window
    daily_rows = db._conn.execute(
        "SELECT * FROM temporal_narratives "
        "WHERE kind = 'daily' "
        "  AND period_start >= ? AND period_start < ? "
        "ORDER BY period_start ASC",
        (period_start, period_end),
    ).fetchall()
    dailies = [dict(r) for r in daily_rows]

    human_entries = db.human_journal_entries_in_window(
        start_utc_iso=period_start, end_utc_iso=period_end,
    )

    # Active this week
    active_rows = db._conn.execute(
        "SELECT * FROM project_summaries "
        "WHERE last_active_at >= ? AND last_active_at < ? "
        "ORDER BY active_minutes_total DESC LIMIT 10",
        (period_start, period_end),
    ).fetchall()
    active = [dict(r) for r in active_rows]
    active_set = {p["project"] for p in active}

    # Stalled = was active in last 30d but NOT this week
    from datetime import datetime, timedelta, timezone
    thirty_d_ago = (datetime.now(timezone.utc) - timedelta(days=30)
                    ).strftime("%Y-%m-%d %H:%M:%S")
    stalled_rows = db._conn.execute(
        "SELECT * FROM project_summaries "
        "WHERE last_active_at >= ? AND last_active_at < ? "
        "ORDER BY active_minutes_total DESC LIMIT 10",
        (thirty_d_ago, period_start),
    ).fetchall()
    stalled = [dict(r) for r in stalled_rows
               if r["project"] not in active_set][:8]

    # Questions with new evidence this week
    try:
        q_rows = db._conn.execute(
            "SELECT * FROM open_questions "
            "WHERE lifecycle = 'active' "
            "  AND last_evidence_at >= ? AND last_evidence_at < ? "
            "ORDER BY evidence_count DESC LIMIT 8",
            (period_start, period_end),
        ).fetchall()
        questions = [dict(r) for r in q_rows]
    except Exception:
        questions = []

    # Slice 14.7.3 (2026-05-26): category breakdown for [WORK] /
    # [CORTEX] / [PERSONAL] section split. Count sessions per
    # category in the window; also annotate each active project
    # with its dominant category (most common across its sessions
    # this window). Empty category column → 'unclassified'.
    category_counts = _category_counts_in_window(
        db, period_start, period_end)
    project_categories = _project_dominant_categories(
        db, period_start, period_end)
    # Decorate active + stalled rows with category for the formatter
    for p in active:
        p["_dominant_category"] = project_categories.get(
            p.get("project", ""), "unclassified")
    for p in stalled:
        p["_dominant_category"] = project_categories.get(
            p.get("project", ""), "unclassified")

    return {
        "dailies": dailies,
        "human_entries": human_entries,
        "active": active,
        "stalled": stalled,
        "questions": questions,
        "category_counts": category_counts,
        "period_label": period_label,
        "window_end_local": local_now.strftime("%Y-%m-%d"),
    }


# ── Slice 14.7.3 helpers: category breakdown ────────────────────


def _category_counts_in_window(db, period_start, period_end) -> dict:
    """Count imported_sessions per category in the [start, end) window.
    Returns {category: count}; empty/null categories bucketed as
    'unclassified'."""
    rows = db._conn.execute(
        "SELECT COALESCE(NULLIF(category,''),'unclassified') AS cat, "
        "  COUNT(*) AS n FROM imported_sessions "
        "WHERE started_at >= ? AND started_at < ? "
        "GROUP BY cat",
        (period_start, period_end),
    ).fetchall()
    return {r["cat"]: r["n"] for r in rows}


def _project_dominant_categories(db, period_start, period_end) -> dict:
    """For each project that had sessions in the window, return the
    most-common category across those sessions. Tie-break: 'work' >
    'cortex' > 'personal' > 'unclassified' (stricter wins)."""
    rows = db._conn.execute(
        "SELECT project, "
        "  COALESCE(NULLIF(category,''),'unclassified') AS cat, "
        "  COUNT(*) AS n FROM imported_sessions "
        "WHERE started_at >= ? AND started_at < ? "
        "  AND project IS NOT NULL AND project != '' "
        "GROUP BY project, cat",
        (period_start, period_end),
    ).fetchall()
    by_proj: dict = {}
    for r in rows:
        proj, cat, n = r["project"], r["cat"], r["n"]
        by_proj.setdefault(proj, {})[cat] = n
    priority = {"work": 0, "cortex": 1, "personal": 2,
                "unclassified": 3}
    result: dict = {}
    for proj, cats in by_proj.items():
        # Sort by count desc, then by priority asc for tiebreak
        result[proj] = sorted(
            cats.items(),
            key=lambda kv: (-kv[1], priority.get(kv[0], 9)))[0][0]
    return result


def _format_category_breakdown(category_counts: dict) -> str:
    """Render the per-category session count block. Always shows
    all 4 categories so missing ones are visibly zero (helps the
    LLM decide which sections to skip).
    """
    if not category_counts:
        return "  (no session activity recorded in this window)"
    order = ["work", "cortex", "personal", "unclassified"]
    out = []
    for cat in order:
        n = category_counts.get(cat, 0)
        out.append(f"  {cat:14s}  {n:4d} sessions")
    extras = {k: v for k, v in category_counts.items()
              if k not in order}
    for cat, n in sorted(extras.items()):
        out.append(f"  {cat:14s}  {n:4d} sessions  (unexpected)")
    return "\n".join(out)


# ── end Slice 14.7.3 helpers ────────────────────────────────────


def _format_dailies(dailies):
    if not dailies:
        return "  (no daily snapshots this week — loop may not have caught them)"
    out = []
    for d in dailies:
        body = (d.get("narrative") or "").strip().replace("\n\n", " — ")
        if len(body) > 320:
            body = body[:320] + " […]"
        out.append("  ── {} ──\n  {}".format(d.get("period_label", "?"), body))
    return "\n\n".join(out)


def _format_active_projects(active):
    if not active:
        return "    (none)"
    out = []
    for p in active[:10]:
        am_h = int((p.get("active_minutes_total") or 0) / 60)
        sc = int(p.get("session_count") or 0)
        # Slice 14.7.3: include dominant category tag
        cat = p.get("_dominant_category", "unclassified")
        out.append("    - [{}] {}: {}h active, {} session(s)".format(
            cat, p.get("project", "?")[:40], am_h, sc))
    return "\n".join(out)


def _format_stalled_projects(stalled):
    if not stalled:
        return "    (no recent project went quiet this week)"
    out = []
    for p in stalled[:6]:
        last = (p.get("last_active_at") or "")[:10]
        cat = p.get("_dominant_category", "unclassified")
        out.append("    - [{}] {} (last active {})".format(
            cat, p.get("project", "?")[:40], last))
    return "\n".join(out)


def generate_weekly(*, db, llm, period_start, period_end, period_label,
                     local_now, max_cost_usd=None,
                     triggered_by="loop"):
    ctx = gather_weekly_context(
        db=db, period_start=period_start, period_end=period_end,
        period_label=period_label, local_now=local_now,
    )
    prompt = WEEKLY_PROMPT_TEMPLATE.format(
        principle=SHARED_PRINCIPLE,
        period_label=ctx["period_label"],
        window_end_local=ctx["window_end_local"],
        period_start=period_start,
        period_end=period_end,
        n_dailies=len(ctx["dailies"]),
        daily_snapshots_block=_format_dailies(ctx["dailies"]),
        human_entries_block=_format_human_entries(ctx["human_entries"]),
        category_breakdown_block=_format_category_breakdown(
            ctx.get("category_counts", {})),
        active_projects_block=_format_active_projects(ctx["active"]),
        stalled_projects_block=_format_stalled_projects(ctx["stalled"]),
        questions_block=_format_questions(ctx["questions"]),
    )
    return _call_llm(llm=llm, prompt=prompt, kind="weekly",
                     max_cost_usd=max_cost_usd,
                     triggered_by=triggered_by)


# ── MONTHLY ─────────────────────────────────────────────────────


MONTHLY_PROMPT_TEMPLATE = """\
{principle}

You are writing a MONTHLY REVIEW — sectioned reflection on the
past month. Lighter and more durable than weekly. The user will
read this once and may not return to it; write something they'd
want to re-read in a year to remember the month.

PERIOD: {period_label}  (calendar month)
WINDOW: {period_start} → {period_end} UTC

WEEKLY SYNTHESES THIS MONTH ({n_weeklies} weeks have one):
{weekly_block}

YOUR (the user's) JOURNAL ENTRIES THIS MONTH (most recent {n_entries}):
{human_entries_block}

CATEGORY ACTIVITY THIS MONTH:
{category_breakdown_block}

PROJECT MOMENTUM (each tagged with dominant category):
  Most active this month (by active hours):
{active_projects_block}

OPEN QUESTIONS LIFECYCLE THIS MONTH:
{questions_block}

FORMAT — sectioned synthesis, under 400 words total:
Use these section markers EXACTLY when each has activity:

  [WORK]
  Two-three sentences on clinical / an employer / ClientA / ProjectX /
  business/regulatory work. What moved, what stalled, what shifted
  vs prior month. Skip the section entirely if zero work activity.

  [CORTEX]
  Two-three sentences on cortex-core / cortex-desktop / overseer /
  the memory system. Major builds, architectural decisions, slice
  shipments. Skip if zero activity.

  [PERSONAL]
  Two-three sentences on Open Muscle / UAP / TruthSea / personal
  research / curiosity / life. Skip if zero activity.

  [ARC]
  ONE-TWO sentences. The single observation worth carrying forward
  about THIS month — pattern of behavior, theme that strengthened,
  thing that's becoming the signature. Not advice. Just a thing
  worth noticing. Always include this section if any others fired.

CONSTRAINTS:
  • Plain prose within each section. No bullet lists, no sub-headers,
    no emoji.
  • If the entire month was sparse (fewer than 2 weeklies, no
    journal entries), write a single short paragraph (no section
    markers) saying so and stop.
  • Lighter than weekly — the monthly is for orientation, not
    inventory. If you find yourself listing every project under
    [WORK] or [PERSONAL], compress.
  • No advice, no recommendations, no "you should" — observe and
    name.

AUTHORSHIP MARKERS — DO NOT FLATTEN:
If the inputs above contain text matching `[B:<name>]` or
`[C:<name>]`, PRESERVE them verbatim. Stripping them collapses
audit provenance — readers need to tell B/C work apart from the
overseer's own thinking.
"""


def gather_monthly_context(*, db, period_start, period_end, period_label,
                            local_now):
    weekly_rows = db._conn.execute(
        "SELECT * FROM temporal_narratives "
        "WHERE kind = 'weekly' "
        "  AND period_start >= ? AND period_start < ? "
        "ORDER BY period_start ASC",
        (period_start, period_end),
    ).fetchall()
    weeklies = [dict(r) for r in weekly_rows]

    human_entries = db.human_journal_entries_in_window(
        start_utc_iso=period_start, end_utc_iso=period_end, limit=20,
    )

    # Active this month
    active_rows = db._conn.execute(
        "SELECT * FROM project_summaries "
        "WHERE last_active_at >= ? AND last_active_at < ? "
        "ORDER BY active_minutes_total DESC LIMIT 10",
        (period_start, period_end),
    ).fetchall()
    active = [dict(r) for r in active_rows]

    # Question lifecycle changes this month — anything that
    # transitioned to/from 'resolved' within the window. Best-
    # effort; falls back to "active questions with most evidence"
    # if the schema doesn't track lifecycle changes directly.
    try:
        q_rows = db._conn.execute(
            "SELECT * FROM open_questions "
            "WHERE last_evidence_at >= ? AND last_evidence_at < ? "
            "   OR (lifecycle = 'resolved' "
            "       AND last_evidence_at >= ?) "
            "ORDER BY evidence_count DESC LIMIT 8",
            (period_start, period_end, period_start),
        ).fetchall()
        questions = [dict(r) for r in q_rows]
    except Exception:
        questions = []

    # Slice 14.7.3: category breakdown for monthly section split.
    category_counts = _category_counts_in_window(
        db, period_start, period_end)
    project_categories = _project_dominant_categories(
        db, period_start, period_end)
    for p in active:
        p["_dominant_category"] = project_categories.get(
            p.get("project", ""), "unclassified")

    return {
        "weeklies": weeklies,
        "human_entries": human_entries,
        "active": active,
        "questions": questions,
        "category_counts": category_counts,
        "period_label": period_label,
    }


def _format_weeklies(weeklies):
    if not weeklies:
        return "  (no weekly syntheses this month)"
    out = []
    for w in weeklies:
        body = (w.get("narrative") or "").strip().replace("\n\n", " — ")
        if len(body) > 400:
            body = body[:400] + " […]"
        out.append("  ── {} ──\n  {}".format(w.get("period_label", "?"), body))
    return "\n\n".join(out)


def generate_monthly(*, db, llm, period_start, period_end, period_label,
                      local_now, max_cost_usd=None,
                      triggered_by="loop"):
    ctx = gather_monthly_context(
        db=db, period_start=period_start, period_end=period_end,
        period_label=period_label, local_now=local_now,
    )
    prompt = MONTHLY_PROMPT_TEMPLATE.format(
        principle=SHARED_PRINCIPLE,
        period_label=ctx["period_label"],
        period_start=period_start,
        period_end=period_end,
        n_weeklies=len(ctx["weeklies"]),
        weekly_block=_format_weeklies(ctx["weeklies"]),
        n_entries=len(ctx["human_entries"]),
        human_entries_block=_format_human_entries(ctx["human_entries"]),
        category_breakdown_block=_format_category_breakdown(
            ctx.get("category_counts", {})),
        active_projects_block=_format_active_projects(ctx["active"]),
        questions_block=_format_questions(ctx["questions"]),
    )
    return _call_llm(llm=llm, prompt=prompt, kind="monthly",
                     max_cost_usd=max_cost_usd,
                     triggered_by=triggered_by)


# ── Shared LLM call + persistence ───────────────────────────────


def _call_llm(*, llm, prompt, kind, max_cost_usd, triggered_by):
    cap = (max_cost_usd if max_cost_usd is not None
           else DEFAULT_MAX_COST_USD_PER_CALL)
    t0 = time.monotonic()
    result = llm.complete(
        prompt,
        purpose="temporal-{}".format(kind),
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    cost = float(result.get("cost_usd") or 0.0)
    if cost > cap:
        log.warning("temporal-%s cost $%.4f exceeded cap $%.4f (model=%s)",
                    kind, cost, cap, result.get("model"))
    if not result.get("ok"):
        return {
            "ok": False, "kind": kind,
            "error": result.get("error", "llm error"),
            "cost_usd": cost,
            "latency_ms": elapsed_ms,
        }
    return {
        "ok": True,
        "kind": kind,
        "narrative": (result.get("text") or "").strip(),
        "model": result.get("model", ""),
        "backend": result.get("backend", ""),
        "cost_usd": cost,
        "latency_ms": elapsed_ms,
        "prompt_tokens": result.get("prompt_tokens", 0),
        "completion_tokens": result.get("completion_tokens", 0),
        "triggered_by": triggered_by,
    }


def apply_temporal_narrative(*, db, gen_result, period_start, period_end,
                               period_label, local_created_at):
    """Persist a generated narrative into temporal_narratives. Returns
    the new row id, or None if the UNIQUE(kind, period_label) gate
    rejected (someone else generated the same period in between).
    """
    return db.add_temporal_narrative(
        kind=gen_result["kind"],
        period_start=period_start,
        period_end=period_end,
        period_label=period_label,
        narrative=gen_result["narrative"],
        cost_usd=gen_result.get("cost_usd", 0),
        model=gen_result.get("model", ""),
        triggered_by=gen_result.get("triggered_by", "loop"),
        local_created_at=local_created_at,
    )


# ── YEARLY ──────────────────────────────────────────────────────


YEARLY_PROMPT_TEMPLATE = """\
{principle}

You are writing a YEARLY REVIEW — the lightest, longest-arc
narrative the system produces. The user will read this once a
year (or rarely after) to remember the shape of the year that
just closed.

PERIOD: {period_label}  (calendar year)
WINDOW: {period_start} → {period_end} UTC

MONTHLY REVIEWS THIS YEAR ({n_monthlies} of 12 months have one):
{monthly_block}

YOUR (the user's) JOURNAL ENTRIES THIS YEAR (most recent {n_entries}):
{human_entries_block}

CATEGORY ACTIVITY THIS YEAR:
{category_breakdown_block}

PROJECTS THAT MOVED THIS YEAR (top by active hours, tagged with category):
{active_projects_block}

OPEN QUESTIONS THAT GAINED EVIDENCE THIS YEAR:
{questions_block}

FORMAT — sectioned synthesis, under 500 words total:
Use these section markers EXACTLY when each has activity:

  [WORK]
  Two-three sentences on the year's arc in clinical / an employer /
  ClientA / ProjectX / business — what stayed live across months, what
  resolved, what kept showing up. Skip if zero work activity.

  [CORTEX]
  Two-three sentences on the cortex memory system arc — major
  architectural shifts, the slices that defined the year, what
  the system became. Skip if zero activity.

  [PERSONAL]
  Two-three sentences on Open Muscle / UAP / research / personal
  exploration — the threads that lived across months. Skip if
  zero activity.

  [ARC]
  Two-three sentences. What was this YEAR fundamentally about?
  What's the shape of it from a distance? This is the single
  observation you'd want to re-read in 5 years to remember what
  THIS year was. Not a list — a thesis. Always include if any
  other section fired.

  [CARRYING FORWARD]
  ONE sentence. The live threads as they stand at year-end —
  questions still unresolved, projects still active. Not a "next
  year plan." Just what's still moving. Skip if there's nothing
  meaningfully live.

CONSTRAINTS:
  • Plain prose within each section. No bullet lists, no sub-headers,
    no emoji.
  • If the entire year was sparse (fewer than 3 monthlies, no
    journal entries), write a single short paragraph saying so
    and stop.
  • Lighter than monthly even though the window is bigger — the
    yearly compresses 12 months by design.
  • Write in past tense for the year itself; present tense for
    [CARRYING FORWARD].

AUTHORSHIP MARKERS — DO NOT FLATTEN:
If the inputs above contain text matching `[B:<name>]` or
`[C:<name>]`, PRESERVE them verbatim. Stripping them across the
year-long compaction is especially damaging — audit provenance
becomes invisible at the timescale where it matters most.
"""


def gather_yearly_context(*, db, period_start, period_end, period_label,
                            local_now):
    monthly_rows = db._conn.execute(
        "SELECT * FROM temporal_narratives "
        "WHERE kind = 'monthly' "
        "  AND period_start >= ? AND period_start < ? "
        "ORDER BY period_start ASC",
        (period_start, period_end),
    ).fetchall()
    monthlies = [dict(r) for r in monthly_rows]

    human_entries = db.human_journal_entries_in_window(
        start_utc_iso=period_start, end_utc_iso=period_end, limit=40,
    )

    # Active this year
    active_rows = db._conn.execute(
        "SELECT * FROM project_summaries "
        "WHERE last_active_at >= ? AND last_active_at < ? "
        "ORDER BY active_minutes_total DESC LIMIT 12",
        (period_start, period_end),
    ).fetchall()
    active = [dict(r) for r in active_rows]

    # Questions whose evidence accumulated this year
    try:
        q_rows = db._conn.execute(
            "SELECT * FROM open_questions "
            "WHERE last_evidence_at >= ? AND last_evidence_at < ? "
            "ORDER BY evidence_count DESC LIMIT 10",
            (period_start, period_end),
        ).fetchall()
        questions = [dict(r) for r in q_rows]
    except Exception:
        questions = []

    # Slice 14.7.3: category breakdown for yearly section split.
    category_counts = _category_counts_in_window(
        db, period_start, period_end)
    project_categories = _project_dominant_categories(
        db, period_start, period_end)
    for p in active:
        p["_dominant_category"] = project_categories.get(
            p.get("project", ""), "unclassified")

    return {
        "monthlies": monthlies,
        "human_entries": human_entries,
        "active": active,
        "questions": questions,
        "category_counts": category_counts,
        "period_label": period_label,
    }


def _format_monthlies(monthlies):
    if not monthlies:
        return "  (no monthly narratives this year)"
    out = []
    for m in monthlies:
        body = (m.get("narrative") or "").strip().replace("\n\n", " — ")
        if len(body) > 500:
            body = body[:500] + " […]"
        out.append("  ── {} ──\n  {}".format(m.get("period_label", "?"), body))
    return "\n\n".join(out)


def generate_yearly(*, db, llm, period_start, period_end, period_label,
                     local_now, max_cost_usd=None,
                     triggered_by="loop"):
    ctx = gather_yearly_context(
        db=db, period_start=period_start, period_end=period_end,
        period_label=period_label, local_now=local_now,
    )
    prompt = YEARLY_PROMPT_TEMPLATE.format(
        principle=SHARED_PRINCIPLE,
        period_label=ctx["period_label"],
        period_start=period_start,
        period_end=period_end,
        n_monthlies=len(ctx["monthlies"]),
        monthly_block=_format_monthlies(ctx["monthlies"]),
        n_entries=len(ctx["human_entries"]),
        human_entries_block=_format_human_entries(ctx["human_entries"]),
        category_breakdown_block=_format_category_breakdown(
            ctx.get("category_counts", {})),
        active_projects_block=_format_active_projects(ctx["active"]),
        questions_block=_format_questions(ctx["questions"]),
    )
    return _call_llm(llm=llm, prompt=prompt, kind="yearly",
                     max_cost_usd=max_cost_usd,
                     triggered_by=triggered_by)


# ── Monthly gate ───────────────────────────────────────────────


def monthly_should_run(db) -> tuple[bool, str]:
    """Per Tory's locked rule: skip monthly if no daily snapshot in
    the past 14 days. Returns (should, reason)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=MONTHLY_REQUIRES_DAILY_WITHIN_DAYS)
              ).strftime("%Y-%m-%d %H:%M:%S")
    row = db._conn.execute(
        "SELECT id FROM temporal_narratives "
        "WHERE kind = 'daily' AND created_at >= ? LIMIT 1",
        (cutoff,),
    ).fetchone()
    if row is None:
        return (False,
                "no daily snapshot in past {} days — user disengaged"
                .format(MONTHLY_REQUIRES_DAILY_WITHIN_DAYS))
    return (True, "daily activity present in past {} days"
                  .format(MONTHLY_REQUIRES_DAILY_WITHIN_DAYS))


# ── Yearly gate ────────────────────────────────────────────────


def yearly_should_run(db, period_start, period_end) -> tuple[bool, str]:
    """Skip yearly if the year being reviewed had no monthly
    narratives (signals no real activity for the period). Returns
    (should, reason). Bounds are the year-being-reviewed's UTC
    bounds — same shape the gatherer uses."""
    row = db._conn.execute(
        "SELECT COUNT(*) AS n FROM temporal_narratives "
        "WHERE kind = 'monthly' "
        "  AND period_start >= ? AND period_start < ?",
        (period_start, period_end),
    ).fetchone()
    n = int(row["n"]) if row else 0
    if n < YEARLY_REQUIRES_MONTHLY_COUNT:
        return (False,
                "no monthly narratives in year being reviewed")
    return (True, "{} monthlies present for the year".format(n))
