"""Slice 9 Phase 4 (DB land) — voicemail buffer → overseer.db.

Reads the JSONL buffer produced by mine_voicemails.py, inserts each
voicemail into the voicemails table, cross-references phone_calls
(is_voicemail=1) on timestamp proximity to fill sender_phone +
sender_call_id, and sets the two-tier surface flag per the overseer's
Slice 9 rule:

    surfaces_to_working_memory = 1 if
        (sender_phone matches phone_contact AND that contact has
         call_count >= 5)
        OR transcript_chars >= 50

Run on the Pi:
    sudo python3 mine_voicemails_db.py /tmp/voicemails-buffer.jsonl
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

OVERSEER_DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
AGENT_TAG = "voicemail-batch-import"

# Two-tier surface rule.
SURFACE_MIN_CALL_COUNT = 5
SURFACE_MIN_CHARS = 50
TIMESTAMP_MATCH_TOLERANCE_S = 90  # voicemail received_at vs call.start_ts


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS voicemails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL,
    duration_s REAL,
    transcript TEXT,
    transcript_chars INTEGER,
    language TEXT,
    sender_phone TEXT,
    sender_call_id INTEGER,
    surfaces_to_working_memory INTEGER NOT NULL DEFAULT 0,
    transcribed_at TEXT,
    transcribe_latency_ms INTEGER,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    imported_by_agent TEXT
);
CREATE INDEX IF NOT EXISTS voicemails_received ON voicemails(received_at);
CREATE INDEX IF NOT EXISTS voicemails_sender ON voicemails(sender_phone);
"""


def parse_iso(ts: str) -> datetime:
    """ISO 8601 'Z' or with offset → naive UTC datetime for compare."""
    s = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt
    # Convert to UTC, drop tz for sqlite-string compare
    return dt.astimezone(tz=None).replace(tzinfo=None)


def insert_voicemails(db: sqlite3.Connection, buffer: Path) -> int:
    inserted = 0
    with open(buffer, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                db.execute(
                    """
                    INSERT OR IGNORE INTO voicemails
                      (filename, received_at, duration_s,
                       transcript, transcript_chars, language,
                       transcribed_at, transcribe_latency_ms, error,
                       imported_by_agent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["filename"], r["received_at"],
                        r.get("duration_s"),
                        r.get("transcript"),
                        r.get("transcript_chars"),
                        r.get("language"),
                        r.get("transcribed_at"),
                        r.get("transcribe_latency_ms"),
                        r.get("error"),
                        AGENT_TAG,
                    ),
                )
                if db.total_changes:
                    inserted += 1
            except sqlite3.IntegrityError:
                pass
    return inserted


def cross_reference_calls(db: sqlite3.Connection) -> dict:
    """For each voicemail, find the phone_calls row with is_voicemail=1
    whose start_ts is closest to received_at (within tolerance).
    Fill sender_phone + sender_call_id."""
    matched = 0
    unmatched = 0

    voicemails = list(db.execute(
        "SELECT id, received_at FROM voicemails WHERE sender_phone IS NULL"
    ))
    for vm_id, received_at in voicemails:
        try:
            target = parse_iso(received_at)
        except Exception:
            unmatched += 1
            continue
        win_start = (target - timedelta(seconds=TIMESTAMP_MATCH_TOLERANCE_S)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        win_end = (target + timedelta(seconds=TIMESTAMP_MATCH_TOLERANCE_S)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        candidates = list(db.execute(
            """
            SELECT id, remote_phone, start_ts FROM phone_calls
            WHERE is_voicemail=1
              AND start_ts BETWEEN ? AND ?
            """,
            (win_start, win_end),
        ))
        if not candidates:
            unmatched += 1
            continue
        # Pick closest by absolute time delta
        def delta(c):
            try:
                return abs((parse_iso(c[2]) - target).total_seconds())
            except Exception:
                return 1e9
        candidates.sort(key=delta)
        call_id, phone, _ = candidates[0]
        db.execute(
            "UPDATE voicemails SET sender_phone=?, sender_call_id=? WHERE id=?",
            (phone, call_id, vm_id),
        )
        matched += 1
    db.commit()
    return {"matched": matched, "unmatched": unmatched}


def apply_surface_flags(db: sqlite3.Connection) -> dict:
    """Set surfaces_to_working_memory=1 per the overseer's two-tier rule."""
    # Reset all to 0 first (idempotent re-run) — except this is fine because
    # the rule is deterministic from current state.
    db.execute("UPDATE voicemails SET surfaces_to_working_memory=0")
    db.commit()

    # Tier 1: sender_phone matches a phone_contact with call_count >= N
    n_tier1 = db.execute(
        """
        UPDATE voicemails
        SET surfaces_to_working_memory = 1
        WHERE sender_phone IS NOT NULL
          AND sender_phone IN (
              SELECT phone_number FROM phone_contacts WHERE call_count >= ?
          )
        """,
        (SURFACE_MIN_CALL_COUNT,),
    ).rowcount

    # Tier 2: transcript_chars >= N (substantive content even if sender unknown)
    n_tier2 = db.execute(
        """
        UPDATE voicemails
        SET surfaces_to_working_memory = 1
        WHERE surfaces_to_working_memory = 0
          AND transcript_chars >= ?
        """,
        (SURFACE_MIN_CHARS,),
    ).rowcount
    db.commit()

    surfaced_total = db.execute(
        "SELECT COUNT(*) FROM voicemails WHERE surfaces_to_working_memory=1"
    ).fetchone()[0]
    return {
        "tier1_people_match": n_tier1,
        "tier2_length": n_tier2,
        "surfaced_total": surfaced_total,
    }


def upsert_horizon(db: sqlite3.Connection) -> None:
    res = db.execute(
        "SELECT MIN(received_at), MAX(received_at), COUNT(*) FROM voicemails"
    ).fetchone()
    if not res or not res[0]:
        return
    earliest, latest, n = res
    notes = (
        f"Voicemail import. {n} transcribed audio messages. Before "
        f"{earliest[:10]} no voicemail data exists in this layer. "
        f"Many transcripts are <5 chars (whisper hallucinated 'you' "
        f"from near-silence) — surface flags filter those at the "
        f"working-memory boundary."
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
        ("voicemails", earliest, latest, notes),
    )


def main(buffer_path: str) -> int:
    p = Path(buffer_path)
    if not p.is_file():
        print(f"ERR: {buffer_path} not found", file=sys.stderr)
        return 1

    db = sqlite3.connect(OVERSEER_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    print("=== ensuring schema ===")
    db.executescript(SCHEMA_SQL)
    db.commit()

    print("\n=== inserting from buffer ===")
    n_ins = insert_voicemails(db, p)
    db.commit()
    total_in_db = db.execute(
        "SELECT COUNT(*) FROM voicemails"
    ).fetchone()[0]
    print(f"  inserted {n_ins} new rows; voicemails total: {total_in_db}")

    print("\n=== cross-referencing to phone_calls (is_voicemail=1) ===")
    cr = cross_reference_calls(db)
    print(f"  matched={cr['matched']} unmatched={cr['unmatched']}")

    print("\n=== applying two-tier surface flags ===")
    sf = apply_surface_flags(db)
    print(
        f"  tier1 (sender match, call_count>={SURFACE_MIN_CALL_COUNT}): "
        f"{sf['tier1_people_match']}\n"
        f"  tier2 (transcript >= {SURFACE_MIN_CHARS}c): "
        f"{sf['tier2_length']}\n"
        f"  surfaced total: {sf['surfaced_total']} of {total_in_db}"
    )

    print("\n=== writing data_horizons row ===")
    upsert_horizon(db)
    db.commit()

    print("\n=== sample of surfaced voicemails (with sender names where matched) ===")
    for r in db.execute(
        """
        SELECT v.received_at, v.transcript_chars, v.sender_phone,
               COALESCE(pc.display_name, '(unknown)') AS name,
               substr(v.transcript, 1, 100) AS preview
        FROM voicemails v
        LEFT JOIN phone_contacts pc ON pc.phone_number = v.sender_phone
        WHERE v.surfaces_to_working_memory = 1
        ORDER BY v.received_at DESC
        LIMIT 10
        """
    ):
        print(
            f"  [{r[0][:10]}] {r[1]:>4}c  {r[3][:25]:25s} | "
            f"{(r[4] or '').replace(chr(10), ' ')}..."
        )

    print("\nDONE")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
