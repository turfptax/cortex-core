"""CoreMemoryRO — read-only access to cortex.db for the overseer plugin.

Locked design (2026-05-02):
  - cortex.db is the user's body of recorded experience. STAYS PRISTINE.
  - Plugins NEVER write to cortex.db. Open in SQLite read-only mode so
    a coding mistake can't even attempt a write.
  - Overseer's interpretations live in plugins/overseer/data/overseer.db.

This is the implementation that replaces _NullCoreMemoryRO from
plugin_api.py. The plugin builds its own CoreMemoryRO in on_load() and
sets self.api.core_memory = ...; the runtime stub is just a placeholder.

Method shape mirrors what overseer needs to consolidate working memory:
  - recent_notes(...) / recent_sessions(...) / active_projects(...)
  - notes_in_window(start_iso, end_iso) for periodic digests
  - get_stats() for high-level counts
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path


log = logging.getLogger("plugin.overseer.core_memory_ro")


class CoreMemoryRO:
    """Read-only handle on cortex.db.

    Opens via SQLite URI mode=ro so any write attempt raises immediately
    instead of silently mutating the user's source-of-truth DB.
    """

    def __init__(self, db_path):
        self._db_path = str(db_path)
        if not Path(self._db_path).is_file():
            log.warning("cortex.db not found at %s — overseer reads will be empty",
                        self._db_path)
            self._conn = None
            return
        # uri=True + mode=ro means the connection is physically read-only.
        # Any INSERT/UPDATE/DELETE/CREATE raises sqlite3.OperationalError.
        self._conn = sqlite3.connect(
            "file:{}?mode=ro".format(self._db_path),
            uri=True, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def is_open(self):
        return self._conn is not None

    # ── Generic read helpers ────────────────────────────────────

    def query(self, sql, params=()):
        """Run an arbitrary SELECT and return list of dict rows.

        Refuses any non-SELECT query as a guard rail (the read-only
        connection would raise anyway, but this gives a clearer error).
        """
        if self._conn is None:
            return []
        stripped = sql.strip().lstrip("(").lstrip().lower()
        if not stripped.startswith(("select", "with")):
            raise PermissionError("CoreMemoryRO: only SELECT/WITH allowed")
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── Notes ───────────────────────────────────────────────────

    def recent_notes(self, *, limit=20, project=None, note_type=None,
                     since_iso=None):
        if self._conn is None:
            return []
        sql = "SELECT id, content, tags, project, note_type, source, " \
              "session_id, created_at FROM notes"
        params = []
        wheres = []
        if project:
            wheres.append("project = ?")
            params.append(project)
        if note_type:
            wheres.append("note_type = ?")
            params.append(note_type)
        if since_iso:
            wheres.append("created_at >= ?")
            params.append(since_iso)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def notes_in_window(self, start_iso, end_iso, *, limit=500):
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT id, content, tags, project, note_type, created_at "
            "FROM notes WHERE created_at >= ? AND created_at < ? "
            "ORDER BY created_at ASC LIMIT ?",
            (start_iso, end_iso, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def open_reminders(self, *, limit=50):
        return self.recent_notes(limit=limit, note_type="reminder")

    def recent_decisions(self, *, limit=20):
        return self.recent_notes(limit=limit, note_type="decision")

    # ── Sessions ────────────────────────────────────────────────

    def recent_sessions(self, *, limit=10):
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT id, ai_platform, hostname, started_at, ended_at, "
            "summary, projects FROM sessions "
            "ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def active_sessions(self):
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT id, ai_platform, hostname, started_at FROM sessions "
            "WHERE ended_at IS NULL ORDER BY started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def session_by_id(self, session_id):
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── Projects ────────────────────────────────────────────────

    def active_projects(self, *, limit=20):
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT tag, name, status, priority, description, category, "
            "last_touched, total_hours FROM projects "
            "WHERE status = 'active' ORDER BY last_touched DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_projects(self, *, limit=200):
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT tag, name, status, priority, last_touched FROM projects "
            "ORDER BY last_touched DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stats ───────────────────────────────────────────────────

    def get_stats(self):
        if self._conn is None:
            return {}
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM notes) AS notes_total, "
            "(SELECT COUNT(*) FROM sessions) AS sessions_total, "
            "(SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL) AS active_sessions, "
            "(SELECT COUNT(*) FROM projects WHERE status='active') AS active_projects, "
            "(SELECT MAX(created_at) FROM notes) AS latest_note_at, "
            "(SELECT MAX(started_at) FROM sessions) AS latest_session_at"
        ).fetchone()
        return dict(row) if row else {}
