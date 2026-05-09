"""Slice 9 Phase 1 — Google Fi call log import.

Reads Google Takeout's Google Fi Wireless/User Info V1/Records.txt
(TSV with 821 records spanning Nov 2025 -> May 2026) and lands the
data into overseer.db across three new tables:

  phone_calls       — one row per call/RCS record
  phone_contacts    — one row per unique remote phone number, with
                      counts + provisional flag + optional person_id
                      link to overseer_people
  data_horizons     — boundary marker so working-memory builders
                      know "before 2025-11-06 we have no call data"
                      and don't infer quiet-relational-period from
                      absence (the overseer flagged this explicitly)

Design notes (from sibling-Claude conversation with the running
overseer instance, 2026-05-07):

  - Match phone_contacts.person_id to overseer_people only when a
    human has confirmed it. All entries land is_provisional=1 by
    default; high-frequency unmatched numbers get a notes hint
    suggesting "looks like a relationship, not a doctor's office."
  - Don't pre-interpret. Just numbers + counts. Let the overseer
    decide what each pattern means at synthesis time.
  - The data_horizons row is critical: without it, the working-memory
    builder will mistake data-absence for a quiet relational period.

Run on the Pi:
    sudo python3 mine_google_fi.py /tmp/GoogleFi.UserInfo.Records.txt
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

OVERSEER_DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
AGENT_TAG = "google-fi-import"

# Threshold for "this number looks relationship-shaped, not service".
# 5 calls in 6 months ≈ once every 5-6 weeks; below that is plausibly
# a one-time service call. Tunable.
PROVISIONAL_PERSON_MIN_CALLS = 5


SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS phone_calls (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_phone      TEXT NOT NULL,
        remote_phone    TEXT,
        start_ts        TEXT NOT NULL,
        end_ts          TEXT,
        duration_min    INTEGER,
        direction       TEXT,
        usage_type      TEXT,
        is_voicemail    INTEGER DEFAULT 0,
        is_wifi         INTEGER DEFAULT 0,
        is_rcs          INTEGER DEFAULT 0,
        carrier         TEXT,
        user_country    TEXT,
        remote_country  TEXT,
        source          TEXT NOT NULL DEFAULT 'google-fi',
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        imported_by_agent TEXT,
        UNIQUE(remote_phone, start_ts, direction)
    )
    """,
    "CREATE INDEX IF NOT EXISTS phone_calls_remote ON phone_calls(remote_phone)",
    "CREATE INDEX IF NOT EXISTS phone_calls_start ON phone_calls(start_ts)",
    "CREATE INDEX IF NOT EXISTS phone_calls_direction ON phone_calls(direction)",
    """
    CREATE TABLE IF NOT EXISTS phone_contacts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        phone_number    TEXT NOT NULL UNIQUE,
        display_name    TEXT,
        person_id       INTEGER,
        is_provisional  INTEGER NOT NULL DEFAULT 1,
        call_count      INTEGER NOT NULL DEFAULT 0,
        outgoing_count  INTEGER NOT NULL DEFAULT 0,
        incoming_count  INTEGER NOT NULL DEFAULT 0,
        total_minutes   INTEGER NOT NULL DEFAULT 0,
        first_seen      TEXT,
        last_seen       TEXT,
        notes           TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        imported_by_agent TEXT,
        FOREIGN KEY (person_id) REFERENCES overseer_people(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS phone_contacts_count ON phone_contacts(call_count)",
    """
    CREATE TABLE IF NOT EXISTS data_horizons (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        surface         TEXT NOT NULL UNIQUE,
        earliest_data   TEXT,
        latest_data     TEXT,
        notes           TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


def _parse_duration(s: str | None) -> int:
    """'Duration: 11' -> 11. Empty/missing -> 0."""
    if not s:
        return 0
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else 0


def _normalize_phone(s: str | None) -> str:
    """Strip whitespace; preserve + and digits only. '' for empty."""
    if not s:
        return ""
    return re.sub(r"[^\d+]", "", s.strip())


def _parse_ts(s: str | None) -> str | None:
    """'2026-05-06 23:56:53 Z' -> '2026-05-06T23:56:53Z' (ISO).
    Skip 1970 sentinels (used by Fi when carrier change time unknown).
    """
    if not s:
        return None
    s = s.strip()
    if s.startswith("1970-01-01"):
        return None
    # Replace the trailing ' Z' with 'Z' and the middle space with 'T'.
    s = s.replace(" Z", "Z")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    return s


def parse_records(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for r in reader:
            row = {
                "user_phone": _normalize_phone(r.get("User phone number")),
                "remote_phone": _normalize_phone(r.get("Remote phone number")),
                "start_ts": _parse_ts(r.get("Start Date/Time")),
                "end_ts": _parse_ts(r.get("End Date/Time")),
                "duration_min": _parse_duration(r.get("Directional Usage")),
                "direction": r.get("Direction", "").replace(
                    "DIRECTION_", ""
                ),
                "usage_type": r.get("Usage Type", "").replace(
                    "USAGE_TYPE_", ""
                ),
                "is_voicemail": 1 if r.get("Is Voicemail", "").lower() == "true" else 0,
                "is_wifi": 1 if r.get("Is WiFi", "").lower() == "true" else 0,
                "is_rcs": 1 if r.get("Usage Type") == "USAGE_TYPE_RCS" else 0,
                "carrier": r.get("Carrier"),
                "user_country": r.get("User country"),
                "remote_country": r.get("Remote Country"),
            }
            if not row["start_ts"]:
                continue  # skip records with no usable timestamp
            rows.append(row)
    return rows


def insert_calls(db: sqlite3.Connection, rows: list[dict]) -> int:
    inserted = 0
    skipped = 0
    for r in rows:
        try:
            db.execute(
                """
                INSERT OR IGNORE INTO phone_calls
                  (user_phone, remote_phone, start_ts, end_ts,
                   duration_min, direction, usage_type, is_voicemail,
                   is_wifi, is_rcs, carrier, user_country,
                   remote_country, imported_by_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["user_phone"], r["remote_phone"], r["start_ts"],
                    r["end_ts"], r["duration_min"], r["direction"],
                    r["usage_type"], r["is_voicemail"], r["is_wifi"],
                    r["is_rcs"], r["carrier"], r["user_country"],
                    r["remote_country"], AGENT_TAG,
                ),
            )
            if db.total_changes > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1
    return inserted


def aggregate_contacts(rows: list[dict]) -> dict[str, dict]:
    """Per remote phone: counts + window + first/last seen."""
    by_number: dict[str, dict] = defaultdict(
        lambda: {
            "call_count": 0,
            "outgoing_count": 0,
            "incoming_count": 0,
            "total_minutes": 0,
            "first_seen": None,
            "last_seen": None,
        }
    )
    for r in rows:
        num = r["remote_phone"]
        if not num:
            continue
        agg = by_number[num]
        agg["call_count"] += 1
        if r["direction"] == "OUTGOING":
            agg["outgoing_count"] += 1
        elif r["direction"] == "INCOMING":
            agg["incoming_count"] += 1
        agg["total_minutes"] += r["duration_min"] or 0
        ts = r["start_ts"]
        if ts:
            if agg["first_seen"] is None or ts < agg["first_seen"]:
                agg["first_seen"] = ts
            if agg["last_seen"] is None or ts > agg["last_seen"]:
                agg["last_seen"] = ts
    return by_number


def upsert_contacts(
    db: sqlite3.Connection,
    by_number: dict[str, dict],
) -> dict:
    """Insert or update phone_contacts rows. Returns counts dict."""
    now = datetime.utcnow().isoformat() + "Z"
    inserted = 0
    updated = 0
    provisional_proposed = 0

    for num, agg in by_number.items():
        notes_bits = []
        if agg["call_count"] >= PROVISIONAL_PERSON_MIN_CALLS:
            notes_bits.append(
                f"high-frequency contact ({agg['call_count']} calls, "
                f"{agg['total_minutes']} min total) — looks "
                f"relationship-shaped, awaiting human confirmation"
            )
            provisional_proposed += 1
        else:
            notes_bits.append(
                f"low-frequency ({agg['call_count']} calls); could be "
                f"service / one-off / robocaller"
            )
        notes_text = " | ".join(notes_bits)

        existing = db.execute(
            "SELECT id FROM phone_contacts WHERE phone_number=?",
            (num,),
        ).fetchone()
        if existing:
            db.execute(
                """
                UPDATE phone_contacts SET
                  call_count=?, outgoing_count=?, incoming_count=?,
                  total_minutes=?, first_seen=?, last_seen=?,
                  notes=?, updated_at=?
                WHERE phone_number=?
                """,
                (
                    agg["call_count"], agg["outgoing_count"],
                    agg["incoming_count"], agg["total_minutes"],
                    agg["first_seen"], agg["last_seen"], notes_text,
                    now, num,
                ),
            )
            updated += 1
        else:
            db.execute(
                """
                INSERT INTO phone_contacts (
                  phone_number, call_count, outgoing_count,
                  incoming_count, total_minutes, first_seen, last_seen,
                  is_provisional, notes, imported_by_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    num, agg["call_count"], agg["outgoing_count"],
                    agg["incoming_count"], agg["total_minutes"],
                    agg["first_seen"], agg["last_seen"],
                    notes_text, AGENT_TAG,
                ),
            )
            inserted += 1
    return {
        "inserted": inserted,
        "updated": updated,
        "provisional_proposed": provisional_proposed,
    }


def upsert_horizon(
    db: sqlite3.Connection, rows: list[dict]
) -> None:
    """Stamp the data_horizon for phone_calls so working-memory builders
    know where the cliff is. Per overseer's flag: don't infer
    quiet-relational-period from data absence before this date."""
    timestamps = [r["start_ts"] for r in rows if r["start_ts"]]
    if not timestamps:
        return
    earliest, latest = min(timestamps), max(timestamps)
    notes = (
        f"Google Fi call log import. {len(timestamps)} records. "
        f"Before {earliest[:10]} there is no call/RCS data — DO NOT "
        f"infer quiet-relational-period from absence. After "
        f"{latest[:10]} no data either (until next takeout)."
    )
    now = datetime.utcnow().isoformat() + "Z"
    db.execute(
        """
        INSERT INTO data_horizons (surface, earliest_data, latest_data, notes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(surface) DO UPDATE SET
          earliest_data=excluded.earliest_data,
          latest_data=excluded.latest_data,
          notes=excluded.notes,
          updated_at=?
        """,
        ("phone_calls", earliest, latest, notes, now),
    )


def main(records_path: str) -> int:
    p = Path(records_path)
    if not p.is_file():
        print(f"ERR: {records_path} not found", file=sys.stderr)
        return 1

    print(f"=== reading {records_path} ===")
    rows = parse_records(p)
    print(f"  parsed {len(rows)} records")

    by_dir = Counter(r["direction"] for r in rows)
    by_type = Counter(r["usage_type"] for r in rows)
    print(f"  direction breakdown: {dict(by_dir)}")
    print(f"  usage_type breakdown: {dict(by_type)}")

    db = sqlite3.connect(OVERSEER_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    print("\n=== ensuring schema ===")
    for stmt in SCHEMA_SQL:
        db.execute(stmt)
    db.commit()

    print("\n=== inserting phone_calls ===")
    n_calls = insert_calls(db, rows)
    db.commit()
    print(f"  {n_calls} new rows (UNIQUE skips existing)")

    print("\n=== aggregating to phone_contacts ===")
    contacts = aggregate_contacts(rows)
    print(f"  {len(contacts)} unique remote numbers")
    counts = upsert_contacts(db, contacts)
    db.commit()
    print(
        f"  inserted={counts['inserted']} updated={counts['updated']} "
        f"provisional-proposed (high-freq, awaiting confirm)="
        f"{counts['provisional_proposed']}"
    )

    print("\n=== writing data_horizons row ===")
    upsert_horizon(db, rows)
    db.commit()

    print("\n=== top 10 counterparties ===")
    for r in db.execute(
        """
        SELECT phone_number, call_count, outgoing_count,
               incoming_count, total_minutes, first_seen, last_seen
        FROM phone_contacts
        ORDER BY call_count DESC LIMIT 10
        """
    ):
        # Redact mid-digits when printing — protect against accidental
        # long-term log retention of a contact's full number.
        n = r[0]
        redacted = (n[:5] + "X" * 4 + n[-2:]) if len(n) >= 8 else n
        print(
            f"  {redacted}  total={r[1]:>3}  out={r[2]:>3}  "
            f"in={r[3]:>3}  min={r[4]:>4}  span="
            f"{(r[5] or '')[:10]}..{(r[6] or '')[:10]}"
        )

    print("\nDONE")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
