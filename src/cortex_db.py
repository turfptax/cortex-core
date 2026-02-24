"""Cortex Core — SQLite persistence layer.

Manages the Cortex knowledge database with 8 tables:
sessions, notes, activities, searches, projects, computers, people, files.

Uses WAL mode for safe concurrent reads during writes.
All timestamps are ISO 8601 UTC via SQLite datetime('now').
"""

import sqlite3
import uuid


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    ai_platform TEXT DEFAULT '',
    hostname TEXT DEFAULT '',
    os_info TEXT DEFAULT '',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    summary TEXT DEFAULT '',
    projects TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',
    project TEXT DEFAULT '',
    note_type TEXT DEFAULT 'note',
    source TEXT DEFAULT 'ble',
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program TEXT NOT NULL,
    details TEXT DEFAULT '',
    file_path TEXT DEFAULT '',
    project TEXT DEFAULT '',
    session_id TEXT,
    duration_min INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    source TEXT DEFAULT '',
    url TEXT DEFAULT '',
    project TEXT DEFAULT '',
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS projects (
    tag TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    priority INTEGER DEFAULT 3,
    description TEXT DEFAULT '',
    collaborators TEXT DEFAULT '',
    last_touched TEXT DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS computers (
    hostname TEXT PRIMARY KEY,
    os TEXT DEFAULT '',
    cpu TEXT DEFAULT '',
    gpu TEXT DEFAULT '',
    ram_gb REAL DEFAULT 0,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now')),
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    role TEXT DEFAULT '',
    email TEXT DEFAULT '',
    projects TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    category TEXT DEFAULT 'uploads',
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    project TEXT DEFAULT '',
    mime_type TEXT DEFAULT '',
    size_bytes INTEGER DEFAULT 0,
    source TEXT DEFAULT 'upload',
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at);
CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id);
CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(note_type);
CREATE INDEX IF NOT EXISTS idx_activities_project ON activities(project);
CREATE INDEX IF NOT EXISTS idx_activities_created ON activities(created_at);
CREATE INDEX IF NOT EXISTS idx_searches_project ON searches(project);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(ended_at);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_created ON files(created_at);
"""


class CortexDB:
    """SQLite persistence layer for the Cortex wearable knowledge system."""

    def __init__(self, db_path):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- Notes ---

    def insert_note(self, content, tags="", project="", note_type="note",
                    source="ble", session_id=None):
        cur = self._conn.execute(
            "INSERT INTO notes (content, tags, project, note_type, source, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, tags, project, note_type, source, session_id),
        )
        self._conn.commit()
        return cur.lastrowid

    # --- Activities ---

    def insert_activity(self, program, details="", file_path="",
                        project="", session_id=None, duration_min=0):
        cur = self._conn.execute(
            "INSERT INTO activities (program, details, file_path, project, "
            "session_id, duration_min) VALUES (?, ?, ?, ?, ?, ?)",
            (program, details, file_path, project, session_id, duration_min),
        )
        self._conn.commit()
        return cur.lastrowid

    # --- Searches ---

    def insert_search(self, query, source="", url="", project="",
                      session_id=None):
        cur = self._conn.execute(
            "INSERT INTO searches (query, source, url, project, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (query, source, url, project, session_id),
        )
        self._conn.commit()
        return cur.lastrowid

    # --- Sessions ---

    def start_session(self, ai_platform="", hostname="", os_info=""):
        session_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO sessions (id, ai_platform, hostname, os_info) "
            "VALUES (?, ?, ?, ?)",
            (session_id, ai_platform, hostname, os_info),
        )
        # Upsert computer record
        if hostname:
            self._conn.execute(
                "INSERT INTO computers (hostname, os) VALUES (?, ?) "
                "ON CONFLICT(hostname) DO UPDATE SET os=excluded.os, "
                "last_seen=datetime('now')",
                (hostname, os_info),
            )
        self._conn.commit()
        return session_id

    def end_session(self, session_id, summary="", projects=""):
        cur = self._conn.execute(
            "UPDATE sessions SET ended_at=datetime('now'), summary=?, projects=? "
            "WHERE id=? AND ended_at IS NULL",
            (summary, projects, session_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # --- Projects ---

    def upsert_project(self, tag, name="", status="active", priority=3,
                       description="", collaborators=""):
        self._conn.execute(
            "INSERT INTO projects (tag, name, status, priority, description, "
            "collaborators) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tag) DO UPDATE SET name=excluded.name, "
            "status=excluded.status, priority=excluded.priority, "
            "description=excluded.description, "
            "collaborators=excluded.collaborators, "
            "last_touched=datetime('now')",
            (tag, name, status, priority, description, collaborators),
        )
        self._conn.commit()
        return tag

    # --- Computers ---

    def register_computer(self, hostname, os="", cpu="", gpu="", ram_gb=0,
                          notes=""):
        self._conn.execute(
            "INSERT INTO computers (hostname, os, cpu, gpu, ram_gb, notes) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(hostname) DO UPDATE SET os=excluded.os, "
            "cpu=excluded.cpu, gpu=excluded.gpu, ram_gb=excluded.ram_gb, "
            "notes=excluded.notes, last_seen=datetime('now')",
            (hostname, os, cpu, gpu, ram_gb, notes),
        )
        self._conn.commit()
        return hostname

    # --- Files ---

    def insert_file(self, filename, category="uploads", description="",
                    tags="", project="", mime_type="", size_bytes=0,
                    source="upload", session_id=None):
        cur = self._conn.execute(
            "INSERT INTO files (filename, category, description, tags, project, "
            "mime_type, size_bytes, source, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, category, description, tags, project,
             mime_type, size_bytes, source, session_id),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_files(self, category=None, project=None, limit=50):
        sql = "SELECT * FROM files"
        params = []
        wheres = []
        if category:
            wheres.append("category = ?")
            params.append(category)
        if project:
            wheres.append("project = ?")
            params.append(project)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_files(self, query, limit=20):
        rows = self._conn.execute(
            "SELECT * FROM files WHERE filename LIKE ? OR description LIKE ? "
            "OR tags LIKE ? ORDER BY created_at DESC LIMIT ?",
            ("%{}%".format(query), "%{}%".format(query),
             "%{}%".format(query), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_file(self, file_id):
        cur = self._conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # --- People ---

    def upsert_person(self, person_id, name="", role="", email="",
                      projects="", notes=""):
        self._conn.execute(
            "INSERT INTO people (id, name, role, email, projects, notes) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "role=excluded.role, email=excluded.email, "
            "projects=excluded.projects, notes=excluded.notes",
            (person_id, name, role, email, projects, notes),
        )
        self._conn.commit()
        return person_id

    # --- Stats / Queries ---

    def get_stats(self):
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM notes) AS notes_total, "
            "(SELECT COUNT(*) FROM activities) AS activities_total, "
            "(SELECT COUNT(*) FROM searches) AS searches_total, "
            "(SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL) AS active_sessions, "
            "(SELECT COUNT(*) FROM sessions) AS sessions_total, "
            "(SELECT COUNT(*) FROM projects) AS projects_total, "
            "(SELECT COUNT(*) FROM files) AS files_total"
        ).fetchone()
        return dict(row)

    def get_recent_notes(self, limit=10, project=None, note_type=None):
        sql = "SELECT * FROM notes"
        params = []
        wheres = []
        if project:
            wheres.append("project = ?")
            params.append(project)
        if note_type:
            wheres.append("note_type = ?")
            params.append(note_type)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_recent_sessions(self, limit=5):
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_projects(self):
        rows = self._conn.execute(
            "SELECT * FROM projects WHERE status='active' "
            "ORDER BY last_touched DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_context(self):
        """Composite query for session startup — returns everything an AI
        needs to understand current state."""
        return {
            "active_projects": self.get_active_projects(),
            "recent_sessions": self.get_recent_sessions(5),
            "recent_notes": self.get_recent_notes(10),
            "pending_reminders": self.get_recent_notes(
                limit=20, note_type="reminder",
            ),
            "recent_decisions": self.get_recent_notes(
                limit=10, note_type="decision",
            ),
            "open_bugs": self.get_recent_notes(limit=20, note_type="bug"),
            "recent_files": self.list_files(limit=10),
            "stats": self.get_stats(),
        }
