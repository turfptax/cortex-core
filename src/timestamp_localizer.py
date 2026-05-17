"""Slice 9.4.1 — Local-with-offset timestamp enforcement.

Single source of truth for the "every timestamp stores both UTC and
local-with-offset" rule (see memory/feedback_time_always_local_with_tz.md).

Both ``cortex_db.CortexDB`` and ``plugins.overseer.overseer_db.OverseerDB``
call ``ensure_local_timestamp_columns(conn)`` at init AFTER their
schema executescript. The function:

  1. Scans every user table for columns matching ``*_at`` of type
     TEXT, except those starting with ``local_`` or paired with an
     existing ``local_<col>_at`` sibling.
  2. ``ALTER TABLE ... ADD COLUMN local_<col>_at TEXT DEFAULT ''``
     (sqlite raises a benign error if it already exists; we swallow it)
  3. ``CREATE TRIGGER tgr_<table>_local_<col>_at_ai AFTER INSERT``
     that auto-populates the local column from the UTC column using
     SQLite's strftime + offset arithmetic. Honors DST automatically
     because ``strftime('%s', ts, 'localtime')`` uses /etc/localtime.
  4. ``CREATE TRIGGER tgr_<table>_local_<col>_at_au AFTER UPDATE``
     that re-derives if the UTC column is changed after insert.
  5. Backfills any rows where local is empty.

Why this lives in a shared module (not inline in each DB class):

  - Same logic for both DBs — DRY
  - Future tables added to either schema get the treatment
    automatically without thinking about timezones (the structural
    fix that backstops the durable rule)
  - Easy to test (single function, single DB connection arg)

Why this runs at every init (not just once):

  - New tables added in a later slice get migrated automatically
    next boot — no separate migration script per slice
  - Idempotent: re-running is a no-op (columns exist, triggers
    DROP+CREATE, backfill UPDATEs WHERE local = '')
  - The cost is small (PRAGMA queries + a few ALTERs that fail
    fast when the column exists)

To disable (testing, debugging): set env var
``CORTEX_DISABLE_TIMESTAMP_LOCALIZER=1`` before init.
"""
from __future__ import annotations

import logging
import os
import sqlite3

log = logging.getLogger("cortex.timestamp_localizer")


# ── The local-from-utc expression (pure SQLite) ───────────────────
#
# Output: '<YYYY-MM-DDTHH:MM:SS>±HH:MM'  e.g. '2026-05-16T13:47:08-05:00'
#
# Uses Pi's /etc/localtime for tz, so DST is automatic. The offset
# arithmetic computes (localtime_epoch - utc_epoch) and formats it
# as ±HH:MM with sign and zero-padding.
#
# {col} is substituted with the column name (or "NEW.colname" inside
# a trigger). Wrap the expression in parens at the call site.
LOCAL_FROM_UTC = (
    "strftime('%Y-%m-%dT%H:%M:%S', {col}, 'localtime') || "
    "printf('%+03d:%02d', "
    "(CAST(strftime('%s', {col}, 'localtime') AS INTEGER) - "
    "CAST(strftime('%s', {col}) AS INTEGER)) / 3600, "
    "ABS((CAST(strftime('%s', {col}, 'localtime') AS INTEGER) - "
    "CAST(strftime('%s', {col}) AS INTEGER)) / 60 % 60))"
)


# Tables/columns to skip — ones whose name matches *_at but aren't
# ISO timestamps. Add here if a future slice adds a column like
# "completed_count_at_last_check" or similar.
EXPLICIT_SKIP: set[tuple[str, str]] = set()


def _is_timestamp_column(name: str, type_: str) -> bool:
    if not name.endswith("_at"):
        return False
    if name.startswith("local_"):
        return False
    if type_ and type_.upper() != "TEXT":
        return False
    return True


def _list_user_tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()]


def _list_columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return list(conn.execute(f"PRAGMA table_info({table})").fetchall())


def _find_timestamp_columns(conn: sqlite3.Connection,
                             table: str) -> list[str]:
    cols = _list_columns(conn, table)
    names = {c[1] for c in cols}
    out = []
    for c in cols:
        name, type_ = c[1], c[2] or ""
        if not _is_timestamp_column(name, type_):
            continue
        if (table, name) in EXPLICIT_SKIP:
            continue
        if f"local_{name}" in names:
            continue
        out.append(name)
    return out


def _add_local_column(conn: sqlite3.Connection,
                       table: str, col: str) -> bool:
    """Returns True if column was added, False if it already existed."""
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


def _backfill(conn: sqlite3.Connection, table: str, col: str) -> int:
    local_col = f"local_{col}"
    expr = LOCAL_FROM_UTC.format(col=col)
    cur = conn.execute(
        f"UPDATE {table} SET {local_col} = {expr} "
        f"WHERE {col} IS NOT NULL "
        f"AND ({local_col} IS NULL OR {local_col} = '')"
    )
    return cur.rowcount


def _create_triggers(conn: sqlite3.Connection,
                      table: str, col: str) -> None:
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


def ensure_local_timestamp_columns(conn: sqlite3.Connection) -> dict:
    """Idempotent: add local_<col>_at columns + triggers for every
    timestamp column on every user table in `conn`'s database.

    Returns a summary dict. Safe to call on every DB init. Cost is
    small after the first run (a few PRAGMA queries + ALTERs that
    fail-fast on duplicate-column).
    """
    if os.environ.get("CORTEX_DISABLE_TIMESTAMP_LOCALIZER"):
        return {"disabled": True}

    summary = {
        "tables_examined": 0,
        "columns_added": 0,
        "columns_already_present": 0,
        "rows_backfilled": 0,
        "triggers_created": 0,
        "errors": [],
    }
    try:
        tables = _list_user_tables(conn)
        summary["tables_examined"] = len(tables)
        for table in tables:
            try:
                cols = _find_timestamp_columns(conn, table)
                for col in cols:
                    try:
                        added = _add_local_column(conn, table, col)
                        if added:
                            summary["columns_added"] += 1
                        else:
                            summary["columns_already_present"] += 1
                        n = _backfill(conn, table, col)
                        summary["rows_backfilled"] += n
                        _create_triggers(conn, table, col)
                        summary["triggers_created"] += 2
                    except Exception as e:
                        msg = f"{table}.{col}: {e}"
                        log.warning("timestamp_localizer: %s", msg)
                        summary["errors"].append(msg)
            except Exception as e:
                msg = f"{table}: {e}"
                log.warning("timestamp_localizer: %s", msg)
                summary["errors"].append(msg)
        conn.commit()
    except Exception as e:
        log.exception("timestamp_localizer top-level: %s", e)
        summary["errors"].append(f"toplevel: {e}")
    if summary["columns_added"] or summary["rows_backfilled"]:
        log.info(
            "timestamp_localizer: +%d cols, %d rows backfilled, "
            "%d triggers, %d tables examined",
            summary["columns_added"], summary["rows_backfilled"],
            summary["triggers_created"], summary["tables_examined"]
        )
    return summary
