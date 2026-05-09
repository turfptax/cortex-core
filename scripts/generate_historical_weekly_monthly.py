"""Generate weekly + monthly retrospective narratives for the
historical Apr 2023 → Mar 2025 period.

Pass 1 — weekly (~104 weeks, 2023-W14 through 2025-W13)
Pass 2 — monthly (24 months, 2023-04 through 2025-03)

Each weekly narrative consumes:
  - All ChatGPT sessions started in that ISO week (titles + gists +
    projects_touched + duration + message_count) from overseer.db
  - tory_life.db time_entries for the week (total minutes,
    per-project breakdown, top categories)
  - tory_life.db purchase_history (notable filter — tech/books/courses)
  - tory_life.db notes
  - tory_life.db browsing top domains (where data exists for that year)

Each monthly narrative additionally consumes:
  - All 4-5 weeklies for that month (from temporal_narratives)
  - Same raw corpus, month-scoped

Period labels match production format:
  weekly:  "2023-W14"     period_start = local-Monday 00:00 (UTC)
  monthly: "2023-04"      period_start = local 1st 00:00 (UTC)

All rows audit-tagged triggered_by='historical-import'.
Idempotent — skips weeks/months already present.

Empty / quiet weeks are NOT skipped; quiet stretches are signal,
and the prompt is told explicitly when a week was quiet.

Run on the Pi:
    sudo python3 generate_historical_weekly_monthly.py \
        /home/turfptax/local_history_ai/tory_life.db
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# cortex-core local imports
sys.path.insert(0, "/home/turfptax/cortex-core/src")
sys.path.insert(0, "/home/turfptax/cortex-core/plugins/overseer")

from llm_router import LLMRouter  # type: ignore
import overseer_db as db_mod  # type: ignore

OVERSEER_DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
LOCAL_TZ = ZoneInfo("America/Chicago")

# Range to backfill (Tory's explicit ask).
WEEK_START = date(2023, 4, 3)   # 2023-W14 Mon (Apr 3 2023)
WEEK_END = date(2025, 3, 31)    # last week starting <= this date
MONTH_START = (2023, 4)
MONTH_END = (2025, 3)

NOTABLE_PURCHASE_HINTS = (
    "kit", "sensor", "module", "esp32", "raspberry", "arduino",
    "book", "course", "subscription", "patent", "domain",
    "filament", "3d", "pcb", "developer", "github",
    "lens", "camera", "microcontroller", "battery", "actuator",
    "laser", "soldering", "oscilloscope", "multimeter", "lab",
)


def is_notable_purchase(item_name: str | None,
                        description: str | None) -> bool:
    blob = ((item_name or "") + " " + (description or "")).lower()
    return any(h in blob for h in NOTABLE_PURCHASE_HINTS)


def local_midnight_utc(d: date) -> datetime:
    """Convert a local-date midnight to UTC datetime (DST-aware)."""
    local_dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=LOCAL_TZ)
    return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def iso_week_label(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def iter_mondays(start: date, end: date):
    """Yield every Monday from `start` (rounded forward) to `end`."""
    d = start
    while d.weekday() != 0:  # 0 = Monday
        d += timedelta(days=1)
    while d <= end:
        yield d
        d += timedelta(days=7)


def iter_months(start_ym, end_ym):
    """Yield (year, month) tuples inclusive."""
    y, m = start_ym
    ey, em = end_ym
    while (y, m) <= (ey, em):
        yield y, m
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


# ---------------------------------------------------------------- weekly

def gather_week_context(
    over: sqlite3.Connection,
    life: sqlite3.Connection,
    week_monday: date,
) -> dict:
    """Collect all signal for a single ISO week (Mon 00:00 → next Mon 00:00 local)."""
    week_start_local = datetime(
        week_monday.year, week_monday.month, week_monday.day,
        0, 0, 0,
    )
    week_end_local = week_start_local + timedelta(days=7)
    # Both DBs store naive timestamps in local-ish ISO. Use the same
    # naive comparison strings the production code uses.
    s_iso = week_start_local.isoformat()
    e_iso = week_end_local.isoformat()

    # ---- ChatGPT sessions started this week (overseer.db)
    sess_rows = list(over.execute(
        """
        SELECT s.id, s.started_at, s.ended_at, s.duration_minutes,
               s.message_count, s.metadata_json,
               g.body AS gist_body
        FROM imported_sessions s
        LEFT JOIN processed_imported_sessions p
                 ON p.imported_id = s.id
        LEFT JOIN summaries_gist g ON g.id = p.gist_id
        WHERE s.source = 'chatgpt'
          AND s.started_at >= ?
          AND s.started_at <  ?
        ORDER BY s.started_at
        """,
        (s_iso, e_iso),
    ))

    sessions = []
    cat_counter: Counter = Counter()
    for r in sess_rows:
        try:
            md = json.loads(r[5] or "{}")
        except Exception:
            md = {}
        title = (md.get("title") or "").strip()
        cats = [
            c.strip()
            for c in (md.get("projects_touched") or "").split(",")
            if c.strip()
        ]
        for c in cats:
            cat_counter[c] += 1
        sessions.append({
            "started_at": r[1],
            "duration_minutes": r[3] or 0,
            "message_count": r[4] or 0,
            "title": title or "(untitled)",
            "categories": cats,
            "gist": (r[6] or "").strip(),
            "tokens": int(md.get("token_count_est") or 0),
        })

    # ---- time tracking (tory_life.db)
    time_rows = list(life.execute(
        """
        SELECT project_id, duration_minutes
        FROM time_entries
        WHERE started_at >= ? AND started_at < ?
          AND duration_minutes IS NOT NULL
        """,
        (s_iso, e_iso),
    ))
    total_tracked = sum(r[1] or 0 for r in time_rows)
    project_min: Counter = Counter()
    for pid, mins in time_rows:
        if pid:
            project_min[pid] += mins or 0

    proj_names = {
        row[0]: row[1]
        for row in life.execute("SELECT id, name FROM projects")
    }
    top_projects = [
        (proj_names.get(pid, f"#{pid}"), mins)
        for pid, mins in project_min.most_common(10)
    ]

    # ---- purchases
    purch_rows = list(life.execute(
        """
        SELECT item_name, description, amount, vendor
        FROM purchase_history
        WHERE purchased_at >= ? AND purchased_at < ?
        """,
        (s_iso, e_iso),
    ))
    notable_purchases = [
        (r[0], r[2], r[3])
        for r in purch_rows
        if is_notable_purchase(r[0], r[1])
    ][:10]

    # ---- notes
    note_rows = list(life.execute(
        """
        SELECT content, project_id
        FROM notes
        WHERE created_at >= ? AND created_at < ?
        ORDER BY created_at
        """,
        (s_iso, e_iso),
    ))
    note_excerpts = [
        ((r[0] or "")[:200], proj_names.get(r[1], ""))
        for r in note_rows[:8]
    ]

    # ---- browsing (top domains)
    dom_counter: Counter = Counter()
    for r in life.execute(
        """
        SELECT domain
        FROM browsing_history
        WHERE visited_at >= ? AND visited_at < ?
        """,
        (s_iso, e_iso),
    ):
        if r[0]:
            dom_counter[r[0]] += 1
    top_domains = dom_counter.most_common(10)

    return {
        "week_label": iso_week_label(week_monday),
        "week_monday": week_monday,
        "week_sunday": week_monday + timedelta(days=6),
        "sessions": sessions,
        "n_sessions": len(sessions),
        "top_categories": cat_counter.most_common(8),
        "total_tracked_minutes": total_tracked,
        "top_projects": top_projects,
        "n_purchases": len(notable_purchases),
        "notable_purchases": notable_purchases,
        "n_notes": len(note_rows),
        "note_excerpts": note_excerpts,
        "top_domains": top_domains,
    }


WEEKLY_PROMPT = """You are the overseer for Tory's memory system, retroactively
writing a week-in-review for a week before the cortex memory system existed.
You're working from structured logs imported from his life database, the
ChatGPT sessions he ran that week, and the gists summarizing each session.

LOCKED PRINCIPLE: you are a quiet memory layer. Capture, surface, connect —
not coach. NO advice, NO "you should have," NO motivational tone. Plain
language, second person ("you"), direct prose only. NO bullet lists. NO
"This week was..." opening. NO "Looking back" framing.

Format: 1-2 paragraphs, ~120-220 words. If the week was genuinely quiet
(few or no sessions), say so plainly in 1-3 sentences and stop — do NOT pad.

Reference specific session titles or project names as evidence; don't
generalize. If a thread continued from a prior week or feeds into a later
week's work, note that briefly.

WEEK: {week_label}  ({week_monday} → {week_sunday})

CHATGPT SESSIONS this week: {n_sessions}
{sessions_block}

CATEGORIES touched ({n_cats}):
{categories_block}

TIME TRACKING: {total_tracked_hours:.1f} hours logged
{top_projects_block}

NOTABLE PURCHASES: {n_purchases}
{purchases_block}

NOTES written: {n_notes}
{notes_block}

BROWSING (top domains, signal of interest):
{domains_block}

Write the week-in-review for {week_label}. Begin directly with the prose."""


def build_weekly_prompt(ctx: dict) -> str:
    sess_lines = []
    if ctx["sessions"]:
        for s in ctx["sessions"][:25]:  # cap at 25; rare to exceed
            cat_str = ", ".join(s["categories"][:3]) if s["categories"] else ""
            line = (
                f'  - "{s["title"]}"'
                f' [{s["message_count"]} msgs, {s["duration_minutes"]}m]'
            )
            if cat_str:
                line += f"  ({cat_str})"
            if s["gist"]:
                line += f"\n      gist: {s['gist']}"
            sess_lines.append(line)
        if len(ctx["sessions"]) > 25:
            sess_lines.append(
                f"  …and {len(ctx['sessions']) - 25} more sessions "
                f"(omitted from prompt for length)"
            )
    sessions_block = "\n".join(sess_lines) or "  (none — no ChatGPT activity this week)"

    cats = ctx["top_categories"]
    cat_lines = "\n".join(f"  - {c}: {n}" for c, n in cats[:8])
    categories_block = cat_lines or "  (none)"

    proj_lines = "\n".join(
        f"  - {name}: {mins/60:.1f}h"
        for name, mins in ctx["top_projects"]
    )
    top_projects_block = proj_lines or "  (no time logged)"

    if ctx["notable_purchases"]:
        purch_lines = "\n".join(
            f"  - {name}  (${amt}, {vendor})"
            for name, amt, vendor in ctx["notable_purchases"]
        )
    else:
        purch_lines = "  (none notable)"

    if ctx["note_excerpts"]:
        note_lines = "\n".join(
            f"  - [{proj}] {body}"
            for body, proj in ctx["note_excerpts"]
        )
    else:
        note_lines = "  (no notes written)"

    if ctx["top_domains"]:
        dom_lines = "\n".join(
            f"  - {dom}: {n}"
            for dom, n in ctx["top_domains"]
        )
    else:
        dom_lines = "  (no browsing data for this period)"

    return WEEKLY_PROMPT.format(
        week_label=ctx["week_label"],
        week_monday=ctx["week_monday"].isoformat(),
        week_sunday=ctx["week_sunday"].isoformat(),
        n_sessions=ctx["n_sessions"],
        sessions_block=sessions_block,
        n_cats=len(cats),
        categories_block=categories_block,
        total_tracked_hours=ctx["total_tracked_minutes"] / 60.0,
        top_projects_block=top_projects_block,
        n_purchases=ctx["n_purchases"],
        purchases_block=purch_lines,
        n_notes=ctx["n_notes"],
        notes_block=note_lines,
        domains_block=dom_lines,
    )


# --------------------------------------------------------------- monthly

def gather_month_context(
    over: sqlite3.Connection,
    life: sqlite3.Connection,
    year: int,
    month: int,
) -> dict:
    """Collect signal for a single calendar month + the weeklies that cover it."""
    m_start = date(year, month, 1)
    m_end = (
        date(year + 1, 1, 1) if month == 12
        else date(year, month + 1, 1)
    )
    s_iso = m_start.isoformat()
    e_iso = m_end.isoformat()

    # ---- ChatGPT sessions started this month
    sess_rows = list(over.execute(
        """
        SELECT s.id, s.started_at, s.duration_minutes, s.message_count,
               s.metadata_json, g.body AS gist
        FROM imported_sessions s
        LEFT JOIN processed_imported_sessions p
                 ON p.imported_id = s.id
        LEFT JOIN summaries_gist g ON g.id = p.gist_id
        WHERE s.source = 'chatgpt'
          AND s.started_at >= ?
          AND s.started_at <  ?
        ORDER BY s.started_at
        """,
        (s_iso, e_iso),
    ))
    sessions = []
    cat_counter: Counter = Counter()
    for r in sess_rows:
        try:
            md = json.loads(r[4] or "{}")
        except Exception:
            md = {}
        title = (md.get("title") or "").strip()
        cats = [
            c.strip()
            for c in (md.get("projects_touched") or "").split(",")
            if c.strip()
        ]
        for c in cats:
            cat_counter[c] += 1
        sessions.append({
            "title": title or "(untitled)",
            "duration_minutes": r[2] or 0,
            "message_count": r[3] or 0,
            "categories": cats,
            "gist": (r[5] or "").strip(),
            "tokens": int(md.get("token_count_est") or 0),
        })

    # rank by token count for "most substantive"
    sessions_by_tokens = sorted(
        sessions, key=lambda s: s["tokens"], reverse=True
    )

    # ---- time tracking
    time_rows = list(life.execute(
        """
        SELECT project_id, duration_minutes
        FROM time_entries
        WHERE started_at >= ? AND started_at < ?
          AND duration_minutes IS NOT NULL
        """,
        (s_iso, e_iso),
    ))
    total_tracked = sum(r[1] or 0 for r in time_rows)
    project_min: Counter = Counter()
    for pid, mins in time_rows:
        if pid:
            project_min[pid] += mins or 0
    proj_names = {
        row[0]: row[1]
        for row in life.execute("SELECT id, name FROM projects")
    }
    top_projects = [
        (proj_names.get(pid, f"#{pid}"), mins)
        for pid, mins in project_min.most_common(15)
    ]

    # ---- purchases
    purch_rows = list(life.execute(
        """
        SELECT item_name, description, amount, vendor
        FROM purchase_history
        WHERE purchased_at >= ? AND purchased_at < ?
        """,
        (s_iso, e_iso),
    ))
    notable_purchases = [
        (r[0], r[2], r[3])
        for r in purch_rows
        if is_notable_purchase(r[0], r[1])
    ][:15]

    # ---- notes
    note_rows = list(life.execute(
        """
        SELECT content, project_id
        FROM notes
        WHERE created_at >= ? AND created_at < ?
        ORDER BY created_at
        """,
        (s_iso, e_iso),
    ))
    note_excerpts = [
        ((r[0] or "")[:200], proj_names.get(r[1], ""))
        for r in note_rows[:10]
    ]

    # ---- browsing
    dom_counter: Counter = Counter()
    for r in life.execute(
        """
        SELECT domain FROM browsing_history
        WHERE visited_at >= ? AND visited_at < ?
        """,
        (s_iso, e_iso),
    ):
        if r[0]:
            dom_counter[r[0]] += 1
    top_domains = dom_counter.most_common(15)

    # ---- weeklies that fall in this month
    # Weekly period_label is "YYYY-Www". Match by period_start
    # falling inside the month bounds.
    weekly_rows = list(over.execute(
        """
        SELECT period_label, narrative
        FROM temporal_narratives
        WHERE kind = 'weekly'
          AND period_start >= ?
          AND period_start <  ?
        ORDER BY period_label
        """,
        (
            local_midnight_utc(m_start).strftime("%Y-%m-%d %H:%M:%S"),
            local_midnight_utc(m_end).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ))

    return {
        "month_label": f"{year:04d}-{month:02d}",
        "year": year, "month": month,
        "n_sessions": len(sessions),
        "sessions_top": sessions_by_tokens[:20],
        "top_categories": cat_counter.most_common(10),
        "total_tracked_minutes": total_tracked,
        "top_projects": top_projects,
        "notable_purchases": notable_purchases,
        "n_purchases": len(notable_purchases),
        "n_notes": len(note_rows),
        "note_excerpts": note_excerpts,
        "top_domains": top_domains,
        "weeklies": weekly_rows,
    }


MONTHLY_PROMPT = """You are the overseer for Tory's memory system, retroactively
writing a month-in-review for a month before the cortex memory system existed.
You have:
  - the four (or five) weekly retrospectives already written for this month;
  - the raw ChatGPT session corpus + gists for the same window;
  - tory_life.db structured signal (time tracking, purchases, notes, browsing).

The weeklies are the per-week ground truth. Your job at the monthly level is
to lift one level: which threads ran across multiple weeks, what arc
played out, what got started or finished or abandoned, what shifted between
weeks. You can quote or paraphrase the weeklies; you can also reach back to
the raw sessions when something the weeklies under-weighted matters.

LOCKED PRINCIPLE: you are a quiet memory layer. Capture, surface, connect —
not coach. NO advice, NO "you should have," NO motivational tone. Plain
language, second person ("you"), direct prose only. NO bullet lists.

Format: 2-3 paragraphs, ~250-400 words. If the month was unusually quiet,
shorter is fine — do NOT pad.

Paragraph 1: the work shape — what dominated attention, what categories
filled the month. Ground in specific session titles or project names.
Paragraph 2: cross-week threads — questions you kept returning to, project
arcs that stretched multi-week, things that started/ended/shifted.
Optional paragraph 3: open questions or unresolved threads at month-end.

MONTH: {month_label}

CHATGPT SESSIONS: {n_sessions} total
Top categories ({n_cats}):
{categories_block}

Most substantive sessions (by length):
{top_sessions_block}

TIME TRACKING: {total_tracked_hours:.1f} hours logged across {n_proj} projects
{top_projects_block}

NOTABLE PURCHASES ({n_purchases}):
{purchases_block}

NOTES ({n_notes} written):
{notes_block}

BROWSING (top domains):
{domains_block}

WEEKLY RETROSPECTIVES already written for this month:
{weeklies_block}

Now write the month-in-review for {month_label}. Begin directly with the prose."""


def build_monthly_prompt(ctx: dict) -> str:
    cats = ctx["top_categories"]
    cat_lines = "\n".join(f"  - {c}: {n} sessions" for c, n in cats[:10])

    sess_lines = []
    for s in ctx["sessions_top"][:20]:
        cat_str = ", ".join(s["categories"][:2]) if s["categories"] else ""
        line = f'  - "{s["title"]}"'
        if s["tokens"]:
            line += f' [{s["tokens"]:,} tokens]'
        if cat_str:
            line += f"  ({cat_str})"
        if s["gist"]:
            line += f"\n      gist: {s['gist'][:200]}"
        sess_lines.append(line)
    top_sessions_block = "\n".join(sess_lines) or "  (none)"

    proj_lines = "\n".join(
        f"  - {name}: {mins/60:.1f}h"
        for name, mins in ctx["top_projects"]
    )

    if ctx["notable_purchases"]:
        purch_lines = "\n".join(
            f"  - {name}  (${amt}, {vendor})"
            for name, amt, vendor in ctx["notable_purchases"]
        )
    else:
        purch_lines = "  (none notable)"

    if ctx["note_excerpts"]:
        note_lines = "\n".join(
            f"  - [{proj}] {body}"
            for body, proj in ctx["note_excerpts"]
        )
    else:
        note_lines = "  (no notes written)"

    dom_lines = "\n".join(
        f"  - {dom}: {n}"
        for dom, n in ctx["top_domains"][:12]
    ) or "  (no browsing data for this period)"

    if ctx["weeklies"]:
        weekly_lines = "\n\n".join(
            f"### {label}\n{narr}"
            for label, narr in ctx["weeklies"]
        )
    else:
        weekly_lines = "  (no weeklies generated for this month — fall back to raw corpus only)"

    return MONTHLY_PROMPT.format(
        month_label=ctx["month_label"],
        n_sessions=ctx["n_sessions"],
        n_cats=len(cats),
        categories_block=cat_lines or "  (none)",
        top_sessions_block=top_sessions_block,
        total_tracked_hours=ctx["total_tracked_minutes"] / 60.0,
        n_proj=len(ctx["top_projects"]),
        top_projects_block=proj_lines or "  (no time logged)",
        n_purchases=ctx["n_purchases"],
        purchases_block=purch_lines,
        n_notes=ctx["n_notes"],
        notes_block=note_lines,
        domains_block=dom_lines,
        weeklies_block=weekly_lines,
    )


# ----------------------------------------------------------------- main

def get_llm() -> "LLMRouter":
    import tomllib
    with open(
        "/home/turfptax/cortex-core/plugins/overseer/plugin.toml", "rb"
    ) as f:
        plugin_toml = tomllib.load(f)
    llm_cfg = plugin_toml.get("llm", {})
    db = db_mod.OverseerDB(OVERSEER_DB)
    return LLMRouter(manifest_llm=llm_cfg, db=db), db


def run_weekly(over: sqlite3.Connection, life: sqlite3.Connection,
               db, llm, only_label: str | None = None) -> dict:
    stats = {"generated": 0, "skipped": 0, "failed": 0,
             "total_cost": 0.0, "labels": []}
    for monday in iter_mondays(WEEK_START, WEEK_END):
        label = iso_week_label(monday)
        if only_label and label != only_label:
            continue
        existing = db.get_temporal_narrative("weekly", label)
        if existing is not None:
            print(f"  [{label}] skip (already exists, "
                  f"triggered_by={existing.get('triggered_by')})")
            stats["skipped"] += 1
            continue

        ctx = gather_week_context(over, life, monday)
        prompt = build_weekly_prompt(ctx)
        print(f"  [{label}] sessions={ctx['n_sessions']:>3} "
              f"hrs={ctx['total_tracked_minutes']/60:>4.1f} "
              f"notes={ctx['n_notes']:>2} "
              f"purch={ctx['n_purchases']:>2} "
              f"prompt={len(prompt):,}c → calling Sonnet…")

        try:
            res = llm.complete(
                prompt=prompt,
                purpose="historical-weekly",
                model="anthropic/claude-sonnet-4.6",
                max_tokens=600,
                temperature=0.4,
            )
        except Exception as e:
            print(f"  [{label}] FAIL llm.complete: {e}")
            stats["failed"] += 1
            continue

        if not res.get("ok"):
            print(f"  [{label}] FAIL non-ok: {res}")
            stats["failed"] += 1
            continue

        narrative = (res.get("text") or "").strip()
        cost = float(res.get("cost_usd") or 0.0)
        stats["total_cost"] += cost
        stats["generated"] += 1
        stats["labels"].append(label)

        period_start_utc = local_midnight_utc(monday)
        period_end_utc = local_midnight_utc(monday + timedelta(days=7))

        new_id = db.add_temporal_narrative(
            kind="weekly",
            period_start=period_start_utc.strftime("%Y-%m-%d %H:%M:%S"),
            period_end=period_end_utc.strftime("%Y-%m-%d %H:%M:%S"),
            period_label=label,
            narrative=narrative,
            cost_usd=cost,
            model=res.get("model") or "anthropic/claude-sonnet-4.6",
            triggered_by="historical-import",
            local_created_at=datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
        )
        print(f"  [{label}] inserted row id={new_id} "
              f"({len(narrative)}c, ${cost:.4f})")
    return stats


def run_monthly(over: sqlite3.Connection, life: sqlite3.Connection,
                db, llm, only_label: str | None = None) -> dict:
    stats = {"generated": 0, "skipped": 0, "failed": 0,
             "total_cost": 0.0, "labels": []}
    for year, month in iter_months(MONTH_START, MONTH_END):
        label = f"{year:04d}-{month:02d}"
        if only_label and label != only_label:
            continue
        existing = db.get_temporal_narrative("monthly", label)
        if existing is not None:
            print(f"  [{label}] skip (already exists, "
                  f"triggered_by={existing.get('triggered_by')})")
            stats["skipped"] += 1
            continue

        ctx = gather_month_context(over, life, year, month)
        prompt = build_monthly_prompt(ctx)
        print(f"  [{label}] sessions={ctx['n_sessions']:>3} "
              f"hrs={ctx['total_tracked_minutes']/60:>5.1f} "
              f"weeklies={len(ctx['weeklies'])} "
              f"prompt={len(prompt):,}c → calling Sonnet…")

        try:
            res = llm.complete(
                prompt=prompt,
                purpose="historical-monthly",
                model="anthropic/claude-sonnet-4.6",
                max_tokens=900,
                temperature=0.4,
            )
        except Exception as e:
            print(f"  [{label}] FAIL llm.complete: {e}")
            stats["failed"] += 1
            continue

        if not res.get("ok"):
            print(f"  [{label}] FAIL non-ok: {res}")
            stats["failed"] += 1
            continue

        narrative = (res.get("text") or "").strip()
        cost = float(res.get("cost_usd") or 0.0)
        stats["total_cost"] += cost
        stats["generated"] += 1
        stats["labels"].append(label)

        m_start = date(year, month, 1)
        m_end = (
            date(year + 1, 1, 1) if month == 12
            else date(year, month + 1, 1)
        )
        new_id = db.add_temporal_narrative(
            kind="monthly",
            period_start=local_midnight_utc(m_start)
                          .strftime("%Y-%m-%d %H:%M:%S"),
            period_end=local_midnight_utc(m_end)
                        .strftime("%Y-%m-%d %H:%M:%S"),
            period_label=label,
            narrative=narrative,
            cost_usd=cost,
            model=res.get("model") or "anthropic/claude-sonnet-4.6",
            triggered_by="historical-import",
            local_created_at=datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
        )
        print(f"  [{label}] inserted row id={new_id} "
              f"({len(narrative)}c, ${cost:.4f})")
    return stats


def main(life_db_path: str, mode: str = "all",
         only_label: str | None = None) -> int:
    if not Path(life_db_path).is_file():
        print(f"ERR: {life_db_path} not found", file=sys.stderr)
        return 1

    life = sqlite3.connect(life_db_path)
    over = sqlite3.connect(OVERSEER_DB)
    over.execute("PRAGMA journal_mode=WAL")
    over.execute("PRAGMA foreign_keys=ON")
    llm, db = get_llm()

    print(f"=== mode={mode} only={only_label or '(all)'} ===")

    if mode in ("weekly", "all"):
        print("\n=== WEEKLY pass ===")
        ws = run_weekly(over, life, db, llm, only_label=only_label)
        print(f"\n weekly: gen={ws['generated']} skip={ws['skipped']} "
              f"fail={ws['failed']} cost=${ws['total_cost']:.4f}")

    if mode in ("monthly", "all"):
        print("\n=== MONTHLY pass ===")
        ms = run_monthly(over, life, db, llm, only_label=only_label)
        print(f"\n monthly: gen={ms['generated']} skip={ms['skipped']} "
              f"fail={ms['failed']} cost=${ms['total_cost']:.4f}")

    print("\nDONE")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    life_path = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "all"
    only = sys.argv[3] if len(sys.argv) > 3 else None
    sys.exit(main(life_path, mode, only))
