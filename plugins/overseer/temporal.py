"""Temporal helpers for Slice 5 (cadence + temporal narratives).

The Pi runs in America/Chicago by default, but the only thing this
module assumes is that the host's `localtime` is set correctly.
That's already a foundational requirement (Tory's CP1 ask).

Two layers:

  - `now_local()` / `now_utc()` — the current time in each frame.
  - period bound helpers — `today_local_bounds()`,
    `week_local_bounds()`, `month_local_bounds()` — return UTC
    timestamps suitable for `WHERE created_at BETWEEN ?` queries
    against tables whose timestamps are UTC ISO strings, but bound
    to LOCAL day/week/month boundaries (the user's mental model).

Period labels:
  daily   → 'YYYY-MM-DD'   (the local date)
  weekly  → 'YYYY-W##'     (ISO week, Monday-anchored)
  monthly → 'YYYY-MM'      (the local year-month)

Note on week anchor: ISO week starts on Monday and runs Mon→Sun.
Tory's spec says "Sunday 10pm" for the weekly trigger — meaning
Sunday is the END of the week and the trigger fires AFTER the week
has fully closed. So a "weekly" narrative covers the seven days
ending on the Sunday it was generated.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    """Current time in the host's local TZ. Uses datetime.astimezone()
    with no argument, which converts to the system local TZ."""
    return datetime.now(timezone.utc).astimezone()


def format_local_iso(dt: datetime | None = None) -> str:
    """ISO 8601 with offset, like '2026-05-03T22:00:00-05:00'.
    Stored alongside UTC timestamps on temporal artifacts so 'Wed for
    me' stays Wed even if the host TZ ever changes."""
    if dt is None:
        dt = now_local()
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def format_utc_iso(dt: datetime | None = None) -> str:
    """SQL-friendly UTC: 'YYYY-MM-DD HH:MM:SS' (no Z, no offset).
    Matches what `datetime('now')` produces in SQLite — good for
    comparing against existing `created_at` columns."""
    if dt is None:
        dt = now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── Local day boundaries ────────────────────────────────────────


def today_local_bounds(local_now: datetime | None = None
                       ) -> tuple[str, str, str]:
    """Returns (period_start_utc, period_end_utc, period_label) for
    'today' in the local TZ. period_start is the local-midnight that
    just passed; period_end is the local-midnight coming up. Both
    are UTC ISO strings suitable for WHERE BETWEEN.

    period_label: local date, e.g. '2026-05-03'.
    """
    if local_now is None:
        local_now = now_local()
    tz = local_now.tzinfo
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        format_utc_iso(start_local),
        format_utc_iso(end_local),
        local_now.strftime("%Y-%m-%d"),
    )


def yesterday_local_bounds(local_now: datetime | None = None
                            ) -> tuple[str, str, str]:
    """Same shape as today_local_bounds but for the previous local
    day. Used by the daily snapshot which fires at 22:00 to cover
    'today so far' OR (when generated late after midnight) 'the day
    that just ended'."""
    if local_now is None:
        local_now = now_local()
    tz = local_now.tzinfo
    yesterday = local_now.date() - timedelta(days=1)
    start_local = datetime.combine(yesterday, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        format_utc_iso(start_local),
        format_utc_iso(end_local),
        yesterday.strftime("%Y-%m-%d"),
    )


def week_local_bounds(local_now: datetime | None = None
                       ) -> tuple[str, str, str]:
    """Returns (period_start_utc, period_end_utc, period_label) for
    the 7-day window ENDING on the most recent Sunday at local-midnight.
    Per Tory's spec the weekly fires Sunday 10pm — at trigger time the
    period it covers is Mon→Sun of the just-finished week.

    If called on a Sunday before/at 22:00, this returns the *current*
    Sunday-ended week (i.e., the 7 days ending later today).
    If called on Monday, it covers the same 7 days that ended yesterday.
    """
    if local_now is None:
        local_now = now_local()
    tz = local_now.tzinfo
    # ISO weekday: Mon=1, Sun=7.
    iso_dow = local_now.isoweekday()
    days_back_to_monday = iso_dow - 1
    monday = local_now.date() - timedelta(days=days_back_to_monday)
    start_local = datetime.combine(monday, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=7)   # next Monday 00:00
    iso_year, iso_week, _ = local_now.isocalendar()
    label = "{}-W{:02d}".format(iso_year, iso_week)
    return (
        format_utc_iso(start_local),
        format_utc_iso(end_local),
        label,
    )


def month_local_bounds(local_now: datetime | None = None
                        ) -> tuple[str, str, str]:
    """Returns (period_start_utc, period_end_utc, period_label) for
    the calendar month containing local_now."""
    if local_now is None:
        local_now = now_local()
    tz = local_now.tzinfo
    first_of_this_month = local_now.date().replace(day=1)
    if first_of_this_month.month == 12:
        first_of_next = first_of_this_month.replace(
            year=first_of_this_month.year + 1, month=1)
    else:
        first_of_next = first_of_this_month.replace(
            month=first_of_this_month.month + 1)
    start_local = datetime.combine(first_of_this_month, time.min, tzinfo=tz)
    end_local = datetime.combine(first_of_next, time.min, tzinfo=tz)
    label = local_now.strftime("%Y-%m")
    return (
        format_utc_iso(start_local),
        format_utc_iso(end_local),
        label,
    )


def previous_month_local_bounds(local_now: datetime | None = None
                                  ) -> tuple[str, str, str]:
    """Bounds for the month JUST CLOSED. Used by the monthly trigger
    on the 1st — at that point the user wants a review of the month
    that ended yesterday, not the empty current month."""
    if local_now is None:
        local_now = now_local()
    tz = local_now.tzinfo
    first_of_this_month = local_now.date().replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    start_local = datetime.combine(first_of_prev, time.min, tzinfo=tz)
    end_local = datetime.combine(first_of_this_month, time.min, tzinfo=tz)
    label = first_of_prev.strftime("%Y-%m")
    return (
        format_utc_iso(start_local),
        format_utc_iso(end_local),
        label,
    )


def previous_year_local_bounds(local_now: datetime | None = None
                                 ) -> tuple[str, str, str]:
    """Bounds for the calendar year JUST CLOSED. Used by the yearly
    trigger on Jan 1 — at that point the user wants a review of the
    year that ended yesterday, not the near-empty current year."""
    if local_now is None:
        local_now = now_local()
    tz = local_now.tzinfo
    prev_year = local_now.year - 1
    start_local = datetime(prev_year, 1, 1, tzinfo=tz)
    end_local = datetime(prev_year + 1, 1, 1, tzinfo=tz)
    label = "{}".format(prev_year)
    return (
        format_utc_iso(start_local),
        format_utc_iso(end_local),
        label,
    )


# ── Trigger predicates ──────────────────────────────────────────
#
# All cadence triggers are anchored to local-22:00 (10pm). The loop
# tick fires every few minutes, so each predicate just answers
# "should this kind run NOW?" — the dedup logic (don't double-
# generate the same period) lives in the caller alongside a
# "is there already a row for this period_label?" check.


TRIGGER_HOUR_LOCAL = 22


def should_attempt_daily(local_now: datetime | None = None) -> bool:
    """Past 22:00 local on any day."""
    if local_now is None:
        local_now = now_local()
    return local_now.hour >= TRIGGER_HOUR_LOCAL


def should_attempt_weekly(local_now: datetime | None = None) -> bool:
    """Past 22:00 local on a Sunday — week-end anchor per spec."""
    if local_now is None:
        local_now = now_local()
    return (local_now.isoweekday() == 7
            and local_now.hour >= TRIGGER_HOUR_LOCAL)


def should_attempt_monthly(local_now: datetime | None = None) -> bool:
    """Past 22:00 local on the 1st — covers the month that just ended."""
    if local_now is None:
        local_now = now_local()
    return (local_now.day == 1
            and local_now.hour >= TRIGGER_HOUR_LOCAL)


def should_attempt_yearly(local_now: datetime | None = None) -> bool:
    """Past 22:00 local on Jan 1 — covers the year that just ended.
    Collides with the monthly trigger (which also fires on the 1st),
    but the loop's "one kind per tick" rule means yearly will fire
    on a subsequent tick within the same trigger window."""
    if local_now is None:
        local_now = now_local()
    return (local_now.month == 1
            and local_now.day == 1
            and local_now.hour >= TRIGGER_HOUR_LOCAL)
