"""Parse a Google Takeout (Google Health / Fitbit) zip into daily
health rollups for the Pi's health_daily table.

Usage:
    python fitbit_takeout_ingest.py <takeout.zip> <out.json>

Produces a JSON array of {day, metric, value} rows. Daily grain only;
raw intraday samples (heart rate, SpO2, HRV) are deliberately not
ingested - the D20/Simples consumers want days, and per-minute series
would bloat the Pi SQLite by orders of magnitude for no reader.

Metrics emitted (source fixed as fitbit-takeout by the loader):
  steps, calories, sedentary_minutes, lightly_active_minutes,
  moderately_active_minutes, very_active_minutes,
  sleep_minutes, time_in_bed, sleep_deep_minutes, sleep_rem_minutes,
  sleep_score, resting_hr, stress_score, azm_minutes

Idempotent by design: the loader INSERT OR REPLACEs on
(day, metric, source), so re-running a newer export just extends and
overwrites.
"""

import csv
import io
import json
import sys
import zipfile
from collections import defaultdict

GED = "Global Export Data/"

# Classic-export intraday/daily value files: sum per day.
SUM_METRICS = {
    "steps": "steps",
    "calories": "calories",
    "sedentary_minutes": "sedentary_minutes",
    "lightly_active_minutes": "lightly_active_minutes",
    "moderately_active_minutes": "moderately_active_minutes",
    "very_active_minutes": "very_active_minutes",
}


def us_date_to_iso(s):
    """'01/22/26 06:00:00' or '11/23/25' -> '2026-01-22'."""
    d = s.split(" ")[0]
    mm, dd, yy = d.split("/")
    return f"20{yy}-{mm}-{dd}"


def main(zip_path, out_path):
    days = defaultdict(float)          # (day, metric) -> value
    sleep_logs = {}                    # logId -> log dict
    sleep_scores = {}                  # entry id -> (day, score)

    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()

        def read_json(name):
            with z.open(name) as f:
                return json.load(io.TextIOWrapper(f, encoding="utf-8"))

        def read_csv(name):
            with z.open(name) as f:
                return list(csv.DictReader(
                    io.TextIOWrapper(f, encoding="utf-8-sig")))

        for name in names:
            base = name.rsplit("/", 1)[-1]

            # ── classic export value series (sum per day) ─────────
            if GED in name:
                prefix = base.split("-")[0] if "-" in base else ""
                joined = "_".join(base.split("-")[0:1])
                for file_prefix, metric in SUM_METRICS.items():
                    if base.startswith(file_prefix + "-"):
                        try:
                            for entry in read_json(name):
                                day = us_date_to_iso(entry["dateTime"])
                                days[(day, metric)] += float(
                                    entry.get("value") or 0)
                        except Exception as e:
                            print(f"warn: {base}: {e}", file=sys.stderr)
                        break
                del prefix, joined

                # resting heart rate: daily series, take the value
                if base.startswith("resting_heart_rate-"):
                    try:
                        for entry in read_json(name):
                            v = entry.get("value") or {}
                            val = v.get("value")
                            if val:
                                day = us_date_to_iso(entry["dateTime"])
                                days[(day, "resting_hr")] = round(
                                    float(val), 1)
                    except Exception as e:
                        print(f"warn: {base}: {e}", file=sys.stderr)

                # sleep logs: month-window files overlap; dedupe logId
                if base.startswith("sleep-") and base.endswith(".json"):
                    try:
                        for log in read_json(name):
                            lid = log.get("logId")
                            if lid is not None:
                                sleep_logs[lid] = log
                    except Exception as e:
                        print(f"warn: {base}: {e}", file=sys.stderr)

            # ── sleep score csv (per sleep log) ───────────────────
            elif base == "sleep_score.csv":
                for row in read_csv(name):
                    try:
                        day = (row.get("timestamp") or "")[:10]
                        score = row.get("overall_score")
                        eid = row.get("sleep_log_entry_id")
                        if day and score:
                            sleep_scores[eid] = (day, float(score))
                    except Exception:
                        continue

            # ── stress score csv (daily) ──────────────────────────
            elif base == "Stress Score.csv":
                for row in read_csv(name):
                    try:
                        day = (row.get("DATE") or "")[:10]
                        score = float(row.get("STRESS_SCORE") or 0)
                        if day and score > 0:
                            days[(day, "stress_score")] = score
                    except Exception:
                        continue

            # ── active zone minutes (per-minute rows, sum) ────────
            elif base.startswith("Active Zone Minutes"):
                for row in read_csv(name):
                    try:
                        day = (row.get("date_time") or "")[:10]
                        mins = float(row.get("total_minutes") or 0)
                        if day:
                            days[(day, "azm_minutes")] += mins
                    except Exception:
                        continue

    # Aggregate deduped sleep logs by dateOfSleep.
    for log in sleep_logs.values():
        day = log.get("dateOfSleep") or ""
        if not day:
            continue
        days[(day, "sleep_minutes")] += float(log.get("minutesAsleep") or 0)
        days[(day, "time_in_bed")] += float(log.get("timeInBed") or 0)
        summary = ((log.get("levels") or {}).get("summary") or {})
        for stage, metric in (("deep", "sleep_deep_minutes"),
                              ("rem", "sleep_rem_minutes")):
            mins = ((summary.get(stage) or {}).get("minutes"))
            if mins:
                days[(day, metric)] += float(mins)

    # Sleep score: one main sleep per day; keep the max.
    per_day_score = defaultdict(float)
    for day, score in sleep_scores.values():
        per_day_score[day] = max(per_day_score[day], score)
    for day, score in per_day_score.items():
        days[(day, "sleep_score")] = score

    # Placeholder filter: the classic export pre-fills future / not-worn
    # days as 1440 sedentary minutes with zero activity and no steps.
    # Those aren't data; they'd poison every average. Drop the activity
    # metrics for such days.
    activity = ("sedentary_minutes", "lightly_active_minutes",
                "moderately_active_minutes", "very_active_minutes")
    for (day, metric) in list(days.keys()):
        if metric != "sedentary_minutes":
            continue
        if days[(day, metric)] >= 1439 and not days.get((day, "steps")):
            for m in activity:
                days.pop((day, m), None)

    rows = [{"day": d, "metric": m, "value": round(v, 2)}
            for (d, m), v in sorted(days.items())]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f)

    by_metric = defaultdict(int)
    for r in rows:
        by_metric[r["metric"]] += 1
    print(f"{len(rows)} daily rows, "
          f"{rows[0]['day']} .. {rows[-1]['day']}" if rows else "no rows")
    for m, n in sorted(by_metric.items()):
        print(f"  {m}: {n} days")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
