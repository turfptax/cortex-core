"""Slice 9 Phase 3 — Fitbit body-data import.

Reads the daily-grain Fitbit metrics from a Google Takeout extraction
and lands them into overseer.db across two new tables:

  body_metrics            — one row per local date, with HRV, sleep,
                            respiratory rate, SpO2, wrist temp delta,
                            active minutes, body-response count
  body_response_events    — one row per device-detected stress event
                            (the misnamed "Stress Journal" data,
                            timestamps only — no user moods logged)

Plus a data_horizons row for the body-data boundary so the overseer's
working-memory builder doesn't infer "no body issues" from days where
the device wasn't worn / before the export window.

Per the overseer's design feedback (2026-05-07 Slice 9 chat):
  - DO NOT pre-interpret. Numbers only. Synthesis decides what
    matters.
  - Feed both raw daily metrics AND a journal-adjacency join so
    the overseer can read entries like "Tory wrote Journal #228 on a
    34-HRV morning" — that join is the surface it actually consumes.

Surfaces processed:
  Heart Rate Variability  (Daily HRV Summary CSVs)
  Sleep Score             (single sleep_score.csv)
  Daily Respiratory Rate  (per-day CSVs)
  Daily SpO2              (multi-day-range CSVs)
  Wrist Temperature       (per-day CSVs, minute-level → daily mean)
  Active Zone Minutes     (per-day CSVs, per-minute zones → daily sum)
  Stress Daily Summaries  (body-response timestamps, no moods)

Skipped:
  HRV Details (minute-level)
  CEDA continuous EDA stream (raw electrodermal data, 111k+ rows)
  Sleep Profile (Premium gate, no data exported)
  Stress Score (README only, no data)

Run on the Pi (after SCP-ing the body/ directory to /tmp/fitbit-body/):
    sudo python3 mine_fitbit_body.py /tmp/fitbit-body
"""
from __future__ import annotations

import csv
import glob
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

OVERSEER_DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
AGENT_TAG = "fitbit-body-import"


SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS body_metrics (
        date                  TEXT PRIMARY KEY,
        -- Heart-rate variability
        rmssd                 REAL,
        nremhr                REAL,
        hrv_entropy           REAL,
        -- Sleep score (Fitbit composite)
        sleep_score           INTEGER,
        sleep_composition     INTEGER,
        sleep_revitalization  INTEGER,
        sleep_duration        INTEGER,
        deep_sleep_min        INTEGER,
        sleep_resting_hr      INTEGER,
        sleep_restlessness    REAL,
        -- Respiratory + SpO2
        respiratory_rate      REAL,
        spo2_avg              REAL,
        spo2_low              REAL,
        spo2_high             REAL,
        -- Temperature
        wrist_temp_delta_avg  REAL,
        -- Activity
        active_minutes_total  INTEGER,
        -- Body response events
        body_response_count   INTEGER,
        -- Audit
        created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        imported_by_agent     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS body_response_events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        start_ts          TEXT NOT NULL,
        end_ts            TEXT,
        duration_min      INTEGER,
        logged_mood       TEXT,
        date              TEXT NOT NULL,
        created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        imported_by_agent TEXT,
        UNIQUE(start_ts)
    )
    """,
    "CREATE INDEX IF NOT EXISTS body_response_date ON body_response_events(date)",
]


def _date_only(ts: str) -> str:
    """ISO timestamp -> YYYY-MM-DD."""
    return ts[:10]


def _safe_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _safe_int(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def collect_hrv_daily(root: Path) -> dict[str, dict]:
    """Daily Heart Rate Variability Summary - YYYY-MM-(N).csv.
    Schema: timestamp, rmssd, nremhr, entropy."""
    out: dict[str, dict] = {}
    for f in sorted(root.glob("Daily Heart Rate Variability Summary*.csv")):
        with open(f, encoding="utf-8") as h:
            for r in csv.DictReader(h):
                d = _date_only(r["timestamp"])
                out[d] = {
                    "rmssd": _safe_float(r.get("rmssd")),
                    "nremhr": _safe_float(r.get("nremhr")),
                    "hrv_entropy": _safe_float(r.get("entropy")),
                }
    return out


def collect_sleep_score(root: Path) -> dict[str, dict]:
    """sleep_score.csv — one row per sleep entry. Multiple per day
    possible (naps); we keep the latest entry per date by timestamp."""
    by_date: dict[str, dict] = {}
    by_date_ts: dict[str, str] = {}
    f = root / "sleep_score.csv"
    if not f.exists():
        return by_date
    with open(f, encoding="utf-8") as h:
        for r in csv.DictReader(h):
            ts = r["timestamp"]
            d = _date_only(ts)
            if d in by_date_ts and by_date_ts[d] >= ts:
                continue
            by_date_ts[d] = ts
            by_date[d] = {
                "sleep_score": _safe_int(r.get("overall_score")),
                "sleep_composition": _safe_int(r.get("composition_score")),
                "sleep_revitalization": _safe_int(r.get("revitalization_score")),
                "sleep_duration": _safe_int(r.get("duration_score")),
                "deep_sleep_min": _safe_int(r.get("deep_sleep_in_minutes")),
                "sleep_resting_hr": _safe_int(r.get("resting_heart_rate")),
                "sleep_restlessness": _safe_float(r.get("restlessness")),
            }
    return by_date


def collect_respiratory(root: Path) -> dict[str, dict]:
    """Daily Respiratory Rate Summary - YYYY-MM-DD.csv."""
    out: dict[str, dict] = {}
    for f in sorted(root.glob("Daily Respiratory Rate Summary*.csv")):
        with open(f, encoding="utf-8") as h:
            for r in csv.DictReader(h):
                d = _date_only(r["timestamp"])
                out[d] = {
                    "respiratory_rate": _safe_float(
                        r.get("daily_respiratory_rate")
                    ),
                }
    return out


def collect_spo2(root: Path) -> dict[str, dict]:
    """Daily SpO2 - YYYY-MM-DD-YYYY-MM-DD.csv. Multi-day range files."""
    out: dict[str, dict] = {}
    for f in sorted(root.glob("Daily SpO2*.csv")):
        with open(f, encoding="utf-8") as h:
            for r in csv.DictReader(h):
                d = _date_only(r["timestamp"])
                out[d] = {
                    "spo2_avg": _safe_float(r.get("average_value")),
                    "spo2_low": _safe_float(r.get("lower_bound")),
                    "spo2_high": _safe_float(r.get("upper_bound")),
                }
    return out


def collect_wrist_temp(root: Path) -> dict[str, dict]:
    """Wrist Temperature - YYYY-MM-DD.csv. Minute-level samples
    (recorded_time, temperature in deg-C delta from baseline).
    Aggregate to daily mean."""
    by_date: dict[str, list[float]] = defaultdict(list)
    for f in sorted(root.glob("Wrist Temperature*.csv")):
        with open(f, encoding="utf-8") as h:
            for r in csv.DictReader(h):
                d = _date_only(r["recorded_time"])
                v = _safe_float(r.get("temperature"))
                if v is not None:
                    by_date[d].append(v)
    return {
        d: {"wrist_temp_delta_avg": sum(vals) / len(vals)}
        for d, vals in by_date.items()
        if vals
    }


def collect_active_minutes(root: Path) -> dict[str, dict]:
    """Active Zone Minutes - YYYY-MM-DD.csv. Per-minute zone records;
    sum total minutes across all zones for the date."""
    by_date: dict[str, int] = defaultdict(int)
    for f in sorted(root.glob("Active Zone Minutes*.csv")):
        with open(f, encoding="utf-8") as h:
            for r in csv.DictReader(h):
                d = _date_only(r["date_time"])
                v = _safe_int(r.get("total_minutes"))
                if v:
                    by_date[d] += v
    return {d: {"active_minutes_total": v} for d, v in by_date.items()}


def collect_body_responses(stress_root: Path) -> tuple[
    list[dict], dict[str, int]
]:
    """Stress Daily Summaries.csv from the stress-journal extraction.
    Returns (events list, count-by-date dict)."""
    events: list[dict] = []
    counts: dict[str, int] = defaultdict(int)
    f = stress_root / "Stress Daily Summaries.csv"
    if not f.exists():
        return events, counts
    with open(f, encoding="utf-8") as h:
        for r in csv.DictReader(h):
            ts = r["body_response_start"]
            d = _date_only(ts)
            try:
                start = datetime.fromisoformat(ts)
                end = datetime.fromisoformat(r["body_response_end"])
                dur = int((end - start).total_seconds() / 60)
            except Exception:
                dur = None
            events.append({
                "start_ts": ts, "end_ts": r["body_response_end"],
                "duration_min": dur,
                "logged_mood": r.get("logged_mood") or "",
                "date": d,
            })
            counts[d] += 1
    return events, dict(counts)


def merge_per_date(*dicts: dict[str, dict]) -> dict[str, dict]:
    """Merge a series of per-date metric dicts into a single per-date row."""
    merged: dict[str, dict] = defaultdict(dict)
    for d in dicts:
        for date, vals in d.items():
            merged[date].update(vals)
    return dict(merged)


COLUMN_ORDER = (
    "rmssd", "nremhr", "hrv_entropy",
    "sleep_score", "sleep_composition", "sleep_revitalization",
    "sleep_duration", "deep_sleep_min", "sleep_resting_hr",
    "sleep_restlessness",
    "respiratory_rate",
    "spo2_avg", "spo2_low", "spo2_high",
    "wrist_temp_delta_avg",
    "active_minutes_total",
    "body_response_count",
)


def upsert_body_metrics(
    db: sqlite3.Connection, merged: dict[str, dict]
) -> dict:
    inserted = 0
    updated = 0
    for date, vals in sorted(merged.items()):
        existing = db.execute(
            "SELECT date FROM body_metrics WHERE date=?", (date,)
        ).fetchone()
        cols = list(COLUMN_ORDER)
        placeholders = ", ".join("?" for _ in cols)
        col_assignments = ", ".join(f"{c}=?" for c in cols)
        values = [vals.get(c) for c in cols]
        if existing:
            db.execute(
                f"UPDATE body_metrics SET {col_assignments}, "
                f"updated_at=CURRENT_TIMESTAMP "
                f"WHERE date=?",
                (*values, date),
            )
            updated += 1
        else:
            db.execute(
                f"INSERT INTO body_metrics "
                f"  (date, {', '.join(cols)}, imported_by_agent) "
                f"VALUES (?, {placeholders}, ?)",
                (date, *values, AGENT_TAG),
            )
            inserted += 1
    return {"inserted": inserted, "updated": updated}


def insert_body_responses(
    db: sqlite3.Connection, events: list[dict]
) -> int:
    inserted = 0
    for e in events:
        try:
            db.execute(
                """
                INSERT OR IGNORE INTO body_response_events
                  (start_ts, end_ts, duration_min, logged_mood, date,
                   imported_by_agent)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    e["start_ts"], e["end_ts"], e["duration_min"],
                    e["logged_mood"], e["date"], AGENT_TAG,
                ),
            )
            if db.total_changes > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    return inserted


def upsert_horizon(
    db: sqlite3.Connection, merged: dict[str, dict]
) -> None:
    if not merged:
        return
    dates = sorted(merged.keys())
    earliest, latest = dates[0], dates[-1]
    notes = (
        f"Fitbit body data import. {len(merged)} day-rows. Before "
        f"{earliest} there is no body data — DO NOT infer health "
        f"baseline from absence. Coverage gaps within the window are "
        f"common (device not worn / not charged); a NULL row does not "
        f"mean Tory was unwell, it means no measurement."
    )
    db.execute(
        """
        INSERT INTO data_horizons (surface, earliest_data, latest_data, notes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(surface) DO UPDATE SET
          earliest_data=excluded.earliest_data,
          latest_data=excluded.latest_data,
          notes=excluded.notes,
          updated_at=CURRENT_TIMESTAMP
        """,
        ("body_metrics", earliest, latest, notes),
    )


def main(extracted_root: str, stress_root: str | None = None) -> int:
    body_root = Path(extracted_root)
    if not body_root.is_dir():
        print(f"ERR: {extracted_root} not found", file=sys.stderr)
        return 1

    print(f"=== reading body data from {body_root} ===")
    hrv = collect_hrv_daily(body_root)
    print(f"  HRV daily: {len(hrv)} dates")
    sleep = collect_sleep_score(body_root)
    print(f"  Sleep score: {len(sleep)} dates")
    resp = collect_respiratory(body_root)
    print(f"  Respiratory rate: {len(resp)} dates")
    spo2 = collect_spo2(body_root)
    print(f"  SpO2: {len(spo2)} dates")
    temp = collect_wrist_temp(body_root)
    print(f"  Wrist temperature: {len(temp)} dates (aggregated from minutes)")
    azm = collect_active_minutes(body_root)
    print(f"  Active minutes: {len(azm)} dates")

    body_events: list[dict] = []
    br_counts: dict[str, int] = {}
    if stress_root:
        sr = Path(stress_root)
        if sr.is_dir():
            body_events, br_counts = collect_body_responses(sr)
            print(f"  Body responses: {len(body_events)} events across "
                  f"{len(br_counts)} dates")

    merged = merge_per_date(
        hrv, sleep, resp, spo2, temp, azm,
        {d: {"body_response_count": n} for d, n in br_counts.items()},
    )
    print(f"\n=== merged: {len(merged)} unique dates ===")

    db = sqlite3.connect(OVERSEER_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    print("\n=== ensuring schema ===")
    for stmt in SCHEMA_SQL:
        db.execute(stmt)
    db.commit()

    print("\n=== upserting body_metrics ===")
    counts = upsert_body_metrics(db, merged)
    db.commit()
    print(f"  inserted={counts['inserted']} updated={counts['updated']}")

    print("\n=== inserting body_response_events ===")
    n_events = insert_body_responses(db, body_events)
    db.commit()
    print(f"  {n_events} new rows")

    print("\n=== writing data_horizons row ===")
    upsert_horizon(db, merged)
    db.commit()

    print("\n=== sample (first + last + 3 random rows) ===")
    for row in db.execute(
        """
        SELECT date, sleep_score, rmssd, respiratory_rate,
               spo2_avg, active_minutes_total, body_response_count
        FROM body_metrics
        ORDER BY date ASC LIMIT 1
        """
    ):
        print(f"  earliest: {dict(zip(['date','sleep','rmssd','rr','spo2','az_min','br_n'], row))}")
    for row in db.execute(
        """
        SELECT date, sleep_score, rmssd, respiratory_rate,
               spo2_avg, active_minutes_total, body_response_count
        FROM body_metrics
        ORDER BY date DESC LIMIT 1
        """
    ):
        print(f"  latest:   {dict(zip(['date','sleep','rmssd','rr','spo2','az_min','br_n'], row))}")
    for row in db.execute(
        """
        SELECT date, sleep_score, rmssd, respiratory_rate,
               spo2_avg, active_minutes_total, body_response_count
        FROM body_metrics
        ORDER BY RANDOM() LIMIT 3
        """
    ):
        print(f"  sample:   {dict(zip(['date','sleep','rmssd','rr','spo2','az_min','br_n'], row))}")

    print("\nDONE")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    extracted = sys.argv[1]
    stress = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(main(extracted, stress))
