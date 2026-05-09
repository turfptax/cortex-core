"""Slice 9 Phase 5 — Contacts → phone_contacts resolution.

Parses Google Takeout's "All Contacts.vcf" (vCard 3.0) from the
second takeout, normalizes phone numbers to the same format used by
phone_contacts, and:

  1. Fills phone_contacts.display_name on every match (exact
     normalized phone).
  2. Clears is_provisional=0 on phone_contacts that now have a name.
  3. Auto-creates provisional overseer_people rows ONLY for matched
     phone_contacts that pass the overseer's tightened gate
     (Slice 9 design chat, 2026-05-07):

         (call_count >= 10 OR total_minutes >= 60)
         AND last_seen within 60 days of now
         AND not a known toll-free / service-prefix pattern

     Provisional people rows get a new column is_provisional=1
     (added via migration here). Tory promotes via direct UPDATE
     when reviewing.

Run on the Pi:
    sudo python3 mine_contacts.py /tmp/All\\ Contacts.vcf
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

OVERSEER_DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
AGENT_TAG = "contacts-import"

# Tightened gate per overseer's Slice 9 feedback.
GATE_MIN_CALLS = 10
GATE_MIN_MINUTES = 60
GATE_RECENCY_DAYS = 60

# Toll-free + service-prefix area codes — exclude before threshold runs.
# Per overseer: dentist's office and Verizon shouldn't pass.
TOLLFREE_AREA = {"800", "833", "844", "855", "866", "877", "888"}
SERVICE_PREFIXES = {"211", "311", "411", "511", "611", "711", "811", "911"}


def normalize_phone(raw: str | None) -> str | None:
    """vCard phone -> '+1XXXXXXXXXX' (E.164 US) or None for short codes."""
    if not raw:
        return None
    # Skip service codes like '#225', '*228', etc.
    if raw.strip().startswith(("#", "*")):
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 12 and digits.startswith("1"):
        # Some takeouts have leading-zero-padded oddities; tolerate.
        return "+" + digits
    if len(digits) >= 7 and len(digits) < 10:
        # Probably a short code or extension — skip
        return None
    if len(digits) > 11 and not digits.startswith("1"):
        # International. Prepend + and keep as-is.
        return "+" + digits
    return None


def is_business_pattern(phone: str) -> bool:
    """E.164 US phone -> True if it's a toll-free or service prefix."""
    if not phone or not phone.startswith("+1") or len(phone) != 12:
        return False
    area = phone[2:5]
    return area in TOLLFREE_AREA or area in SERVICE_PREFIXES


def parse_vcards(path: Path) -> list[dict]:
    """Parse vCard 3.0 file -> list of {name, phones: set[str], categories}."""
    contacts: list[dict] = []
    current: dict | None = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line == "BEGIN:VCARD":
                current = {"name": "", "phones": set(), "categories": ""}
                continue
            if line == "END:VCARD":
                if current and current["phones"]:
                    contacts.append(current)
                current = None
                continue
            if current is None:
                continue
            if line.startswith("FN:"):
                current["name"] = line[3:].strip()
            elif line.startswith("TEL"):
                # TEL[;TYPE=CELL][;...]:NUMBER
                _, _, num = line.partition(":")
                normalized = normalize_phone(num)
                if normalized:
                    current["phones"].add(normalized)
            elif line.startswith("CATEGORIES:"):
                current["categories"] = line[len("CATEGORIES:"):].strip()
    return contacts


def build_phone_to_name(contacts: list[dict]) -> dict[str, str]:
    """Flatten contacts to {phone: name}. If multiple contacts share a
    phone (rare), keep the first non-empty name."""
    by_phone: dict[str, str] = {}
    for c in contacts:
        if not c["name"]:
            continue
        for p in c["phones"]:
            if p not in by_phone:
                by_phone[p] = c["name"]
    return by_phone


def ensure_people_provisional_column(db: sqlite3.Connection) -> None:
    """Add is_provisional column to overseer_people if missing."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(overseer_people)")]
    if "is_provisional" not in cols:
        db.execute(
            "ALTER TABLE overseer_people ADD COLUMN is_provisional "
            "INTEGER NOT NULL DEFAULT 0"
        )
        db.commit()
        print("  + added overseer_people.is_provisional column")


def resolve_contacts(
    db: sqlite3.Connection, phone_to_name: dict[str, str]
) -> dict:
    """Update phone_contacts.display_name from address book matches.
    Returns counts dict."""
    total = 0
    matched = 0
    cleared_provisional = 0

    rows = list(db.execute(
        "SELECT phone_number, is_provisional FROM phone_contacts"
    ))
    for phone, was_provisional in rows:
        total += 1
        name = phone_to_name.get(phone)
        if not name:
            continue
        # Update name + clear provisional
        db.execute(
            "UPDATE phone_contacts SET display_name=?, is_provisional=0, "
            "updated_at=CURRENT_TIMESTAMP WHERE phone_number=?",
            (name, phone),
        )
        matched += 1
        if was_provisional:
            cleared_provisional += 1
    db.commit()
    return {
        "total_contacts": total,
        "matched": matched,
        "cleared_provisional": cleared_provisional,
    }


def select_provisional_person_candidates(
    db: sqlite3.Connection
) -> list[dict]:
    """Apply the tightened gate per overseer's spec."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=GATE_RECENCY_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidates = []
    for row in db.execute(
        """
        SELECT phone_number, display_name, call_count, total_minutes,
               outgoing_count, incoming_count, first_seen, last_seen
        FROM phone_contacts
        WHERE display_name IS NOT NULL AND display_name != ''
          AND (call_count >= ? OR total_minutes >= ?)
          AND last_seen >= ?
        ORDER BY call_count DESC
        """,
        (GATE_MIN_CALLS, GATE_MIN_MINUTES, cutoff),
    ):
        phone = row[0]
        if is_business_pattern(phone):
            continue
        candidates.append({
            "phone_number": phone,
            "name": row[1],
            "call_count": row[2],
            "total_minutes": row[3],
            "outgoing_count": row[4],
            "incoming_count": row[5],
            "first_seen": row[6],
            "last_seen": row[7],
        })
    return candidates


def upsert_provisional_people(
    db: sqlite3.Connection, candidates: list[dict]
) -> dict:
    """Create overseer_people (is_provisional=1) for each candidate
    that doesn't already have one with the same name. Link
    phone_contacts.person_id to the new (or existing) people row."""
    inserted = 0
    linked_existing = 0

    for c in candidates:
        # See if an overseer_people row already exists by name (case-
        # insensitive). If yes, just link the phone_contact to it.
        existing = db.execute(
            "SELECT id FROM overseer_people WHERE LOWER(name)=LOWER(?) LIMIT 1",
            (c["name"],),
        ).fetchone()
        if existing:
            person_id = existing[0]
            linked_existing += 1
        else:
            notes = (
                f"Auto-created provisional from Google Fi call log + "
                f"Contacts. {c['call_count']} calls "
                f"({c['outgoing_count']} out / {c['incoming_count']} in), "
                f"{c['total_minutes']} min total. Span: "
                f"{c['first_seen'][:10]} → {c['last_seen'][:10]}. "
                f"Phone: {c['phone_number']}. Provisional pending "
                f"Tory's confirm."
            )
            handles_json = json.dumps([
                {"kind": "phone", "value": c["phone_number"]},
            ])
            cur = db.execute(
                """
                INSERT INTO overseer_people
                  (name, display_name, online_handles_json,
                   notes, is_provisional, created_by_agent)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    c["name"], c["name"], handles_json, notes,
                    AGENT_TAG,
                ),
            )
            person_id = cur.lastrowid
            inserted += 1
        # Link the phone_contact
        db.execute(
            "UPDATE phone_contacts SET person_id=? WHERE phone_number=?",
            (person_id, c["phone_number"]),
        )
    db.commit()
    return {"inserted": inserted, "linked_existing": linked_existing}


def main(vcard_path: str) -> int:
    p = Path(vcard_path)
    if not p.is_file():
        print(f"ERR: {vcard_path} not found", file=sys.stderr)
        return 1

    print(f"=== parsing {vcard_path} ===")
    contacts = parse_vcards(p)
    print(f"  {len(contacts)} contacts with at least one phone number")

    phone_to_name = build_phone_to_name(contacts)
    print(f"  {len(phone_to_name)} unique phone-to-name mappings")

    db = sqlite3.connect(OVERSEER_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    print("\n=== ensuring people schema ===")
    ensure_people_provisional_column(db)

    print("\n=== resolving phone_contacts ===")
    counts = resolve_contacts(db, phone_to_name)
    print(
        f"  total phone_contacts: {counts['total_contacts']}\n"
        f"  matched to a name:    {counts['matched']}\n"
        f"  was-provisional → confirmed-name: "
        f"{counts['cleared_provisional']}"
    )

    print("\n=== selecting provisional-person candidates ===")
    cands = select_provisional_person_candidates(db)
    print(
        f"  passed tightened gate "
        f"(>= {GATE_MIN_CALLS} calls OR >= {GATE_MIN_MINUTES} min, "
        f"recent within {GATE_RECENCY_DAYS}d, not toll-free): "
        f"{len(cands)}"
    )
    for c in cands:
        # Redact phone in log output
        p = c["phone_number"]
        rp = (p[:5] + "X" * 4 + p[-2:]) if len(p) >= 8 else p
        print(
            f"    {c['name']:30s} {rp}  calls={c['call_count']:>3}  "
            f"min={c['total_minutes']:>4}  last={c['last_seen'][:10]}"
        )

    print("\n=== upserting provisional overseer_people ===")
    pcounts = upsert_provisional_people(db, cands)
    print(
        f"  inserted={pcounts['inserted']} "
        f"linked-to-existing-people={pcounts['linked_existing']}"
    )

    print("\n=== summary of phone_contacts after Phase 5 ===")
    for r in db.execute(
        """
        SELECT
          SUM(CASE WHEN display_name IS NOT NULL AND display_name != '' THEN 1 ELSE 0 END) AS named,
          SUM(CASE WHEN display_name IS NULL OR display_name = '' THEN 1 ELSE 0 END) AS unnamed,
          SUM(CASE WHEN person_id IS NOT NULL THEN 1 ELSE 0 END) AS linked_to_person,
          COUNT(*) AS total
        FROM phone_contacts
        """
    ):
        print(
            f"  named={r[0]} unnamed={r[1]} "
            f"linked-to-person={r[2]} total={r[3]}"
        )

    print("\nDONE")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
