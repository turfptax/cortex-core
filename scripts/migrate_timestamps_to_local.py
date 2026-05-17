"""Slice 9.4.1 — Add local-with-offset timestamps to every cortex table.

Idempotent migration. Runs ON .25. Operates on overseer.db AND cortex.db.

For every timestamp column (any TEXT column whose name ends in `_at`,
excluding ones already starting with `local_`), this script:

  1. ALTERs the table to add a sibling ``local_<col>_at TEXT DEFAULT ''``
  2. BACKFILLs existing rows by converting the stored UTC to local-with-
     offset using SQLite's own strftime (which honors the Pi's
     America/Chicago tz INCLUDING historical DST transitions, so a row
     written in January 2026 gets -06:00 and one in June gets -05:00)
  3. CREATEs an ``AFTER INSERT`` trigger so future INSERTs auto-populate
     the local column. The trigger fires only when the local column is
     empty/null, so writers that DO populate it manually (the
     human_journal_entries pattern) keep working unchanged.
  4. CREATEs an ``AFTER UPDATE`` trigger so if the UTC column is changed
     after insert, the local column re-derives.

The local format is ISO 8601 with explicit offset:
``2026-05-16T13:47:08-05:00`` (matches what temporal_clock.format_local_iso
already produces, which matches human_journal_entries.local_created_at).

Tables that already have a `local_<col>_at` paired with their UTC
column are skipped — these are the known-good reference pattern
(human_journal_entries, temporal_narratives).

Tables/columns explicitly skipped because they're not timestamps even
though their names match the heuristic: NONE found at audit time, but
the EXPLICIT_SKIP set below is the place to add them if found.

Usage on .25 (run AS ROOT since the service runs as root and owns the DBs):
    sudo systemctl stop cortex-core
    sudo cp <db> <db>.bak-tz-migration
    sudo python3 /home/turfptax/cortex-core/scripts/migrate_timestamps_to_local.py
    sudo systemctl start cortex-core
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

# Default DB locations on .25
DEFAULT_DBS = [
    Path("/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"),
    Path("/home/turfptax/cortex-core/cortex.db"),
]

# (table, col) pairs to skip explicitly. Add here if a column ending
# in _at turns out not to be an ISO timestamp.
EXPLICIT_SKIP: set[tuple[str, str]] = set()


# ── The local-from-utc expression ───────────────────────────────────
#
# Pure SQLite, no Python helpers needed. Works inside triggers AND
# in backfill UPDATEs. Honors the Pi's OS tz including DST because
# `strftime('%s', ts, 'localtime')` uses /etc/localtime, which the
# Pi has set to America/Chicago.
#
# Output shape for utc='2026-05-16 18:47:08': '2026-05-16T13:47:08-05:00'
# Output shape for utc='2026-01-15 18:47:08': '2026-01-15T12:47:08-06:00'
#
# The offset arithmetic:
#   localtime epoch - utc epoch = offset in seconds
#   /3600 = hours, /60 % 60 = minutes
#   printf '%+03d:%02d' formats with sign + zero-padding.
LOCAL_FROM_UTC = (
    "strftime('%Y-%m-%dT%H:%M:%S', {col}, 'localtime') || "
    "printf('%+03d:%02d', "
    "(CAST(strftime('%s', {col}, 'localtime') AS INTEGER) - "
    "CAST(strftime('%s', {col}) AS INTEGER)) / 3600, "
    "ABS((CAST(strftime('%s', {col}, 'localtime') AS INTEGER) - "
    "CAST(strftime('%s', {col}) AS INTEGER)) / 60 % 60))"
)


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """User tables only — exclude sqlite_master, sqlite_sequence etc."""
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()]


def list_columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """[(cid, name, type, notnull, dflt, pk), ...] per PRAGMA table_info."""
    return list(conn.execute(f"PRAGMA table_info({table})").fetchall())


def find_timestamp_columns(conn: sqlite3.Connection,
                           table: str) -> list[str]:
    """Return columns that look like ISO timestamps and don't already
    have a `local_<col>` sibling."""
    cols = list_columns(conn, table)
    names = {c[1] for c in cols}
    out = []
    for c in cols:
        name, type_ = c[1], (c[2] or "").upper()
        if not name.endswith("_at"):
            continue
        if name.startswith("local_"):
            continue
        if type_ != "TEXT":
            continue
        if (table, name) in EXPLICIT_SKIP:
            continue
        if f"local_{name}" in names:
            # Already has paired local column — known-good (human_journal,
            # temporal_narratives) or earlier migration result.
            continue
        out.append(name)
    return out


def add_local_column(conn: sqlite3.Connection,
                     table: str, col: str) -> bool:
    """ALTER TABLE … ADD COLUMN local_<col> TEXT DEFAULT ''. Returns True
    if column was actually added, False if it already existed."""
    local_col = f"local_{col}"
    try:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {local_col} TEXT DEFAULT ''"
        )
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            return False
        raise


def backfill(conn: sqlite3.Connection, table: str, col: str) -> int:
    """Populate local_<col> for every row where it's empty AND col is
    not null. Returns count of rows updated. Uses the LOCAL_FROM_UTC
    expression inline so the conversion happens in SQLite and respects
    the Pi's OS tz with DST handling."""
    local_col = f"local_{col}"
    expr = LOCAL_FROM_UTC.format(col=col)
    cur = conn.execute(
        f"UPDATE {table} SET {local_col} = {expr} "
        f"WHERE {col} IS NOT NULL AND ({local_col} IS NULL OR {local_col} = '')"
    )
    return cur.rowcount


def create_triggers(conn: sqlite3.Connection,
                    table: str, col: str) -> None:
    """AFTER INSERT and AFTER UPDATE triggers that auto-populate
    local_<col> from <col>. Idempotent: DROP IF EXISTS first.

    INSERT trigger: fires only when the local column is empty/null. So
    writers that pass the local value explicitly (like
    add_human_journal_entry does today) keep working — they pass the
    value, the trigger sees it's already populated, leaves it alone.

    UPDATE trigger: re-derives local when UTC is changed AND local
    matches the OLD derived value (i.e. it wasn't manually set to
    something else). Catches the rare case of a writer fixing up a
    UTC value after insert.
    """
    local_col = f"local_{col}"
    expr = LOCAL_FROM_UTC.format(col="NEW." + col)
    insert_trigger = f"tgr_{table}_{local_col}_ai"
    update_trigger = f"tgr_{table}_{local_col}_au"

    conn.execute(f"DROP TRIGGER IF EXISTS {insert_trigger}")
    conn.execute(f"""
        CREATE TRIGGER {insert_trigger}
        AFTER INSERT ON {table}
        WHEN NEW.{col} IS NOT NULL
             AND (NEW.{local_col} IS NULL OR NEW.{local_col} = '')
        BEGIN
            UPDATE {table} SET {local_col} = {expr}
            WHERE rowid = NEW.rowid;
        END
    """)

    conn.execute(f"DROP TRIGGER IF EXISTS {update_trigger}")
    conn.execute(f"""
        CREATE TRIGGER {update_trigger}
        AFTER UPDATE OF {col} ON {table}
        WHEN NEW.{col} IS NOT NULL
             AND (NEW.{col} != OLD.{col} OR OLD.{col} IS NULL)
        BEGIN
            UPDATE {table} SET {local_col} = {expr}
            WHERE rowid = NEW.rowid;
        END
    """)


def migrate_db(db_path: Path, dry_run: bool = False) -> dict:
    """Run the full migration on one DB. Returns a summary dict."""
    if not db_path.is_file():
        return {"db": str(db_path), "error": "not found"}

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")  # we're not touching FKs
    summary = {
        "db": str(db_path),
        "tables_examined": 0,
        "columns_examined": 0,
        "columns_added": 0,
        "columns_already_present": 0,
        "rows_backfilled": 0,
        "triggers_created": 0,
        "per_table": [],
    }
    try:
        tables = list_tables(conn)
        summary["tables_examined"] = len(tables)
        for table in tables:
            cols = find_timestamp_columns(conn, table)
            if not cols:
                continue
            table_summary = {
                "table": table, "columns": []}
            for col in cols:
                summary["columns_examined"] += 1
                if dry_run:
                    table_summary["columns"].append(
                        {"col": col, "action": "would-add"})
                    continue
                added = add_local_column(conn, table, col)
                if added:
                    summary["columns_added"] += 1
                else:
                    summary["columns_already_present"] += 1
                n = backfill(conn, table, col)
                summary["rows_backfilled"] += n
                create_triggers(conn, table, col)
                summary["triggers_created"] += 2
                table_summary["columns"].append({
                    "col": col,
                    "added_column": added,
                    "rows_backfilled": n,
                })
            if table_summary["columns"]:
                summary["per_table"].append(table_summary)
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Add local-with-offset timestamp columns + triggers "
                    "to every cortex DB table.")
    parser.add_argument("--db", action="append", type=Path,
                        help="DB path (repeatable). Defaults to the two "
                             "known cortex DBs on .25.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing.")
    args = parser.parse_args()

    dbs = args.db or DEFAULT_DBS
    overall = []
    for db in dbs:
        print(f"\n=== {db} ===")
        s = migrate_db(db, dry_run=args.dry_run)
        for tbl in s.get("per_table", []):
            cols_str = ", ".join(
                f"{c['col']}({'+' if c.get('added_column') else '='}{c.get('rows_backfilled', 0)})"
                for c in tbl["columns"]
            )
            print(f"  {tbl['table']}: {cols_str}")
        print(f"  TOTALS: examined={s.get('tables_examined')} tables, "
              f"{s.get('columns_examined')} cols; "
              f"added={s.get('columns_added')} cols, "
              f"pre-existing={s.get('columns_already_present')}; "
              f"backfilled={s.get('rows_backfilled')} rows; "
              f"triggers={s.get('triggers_created')}.")
        overall.append(s)

    print()
    print("Migration complete." if not args.dry_run else "Dry-run complete.")
    total_added = sum(s.get("columns_added", 0) for s in overall)
    total_backfilled = sum(s.get("rows_backfilled", 0) for s in overall)
    total_triggers = sum(s.get("triggers_created", 0) for s in overall)
    print(f"Across all DBs: +{total_added} columns, "
          f"{total_backfilled} rows backfilled, {total_triggers} triggers.")


if __name__ == "__main__":
    main()
