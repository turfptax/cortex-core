"""Cortex sync plugin: contract v2 over the Pi's LAN HTTP API.

The phone's third sync transport (cortex-desktop/docs/SYNC_CONTRACT_DRAFT.md,
v2 RATIFIED 2026-06-10). Same JSON bodies and reply shapes as the Gateway's
/v1/sync/* and the bridge's CMD:sync_* lines; this is a port of
cortex-gateway/cortex_gateway/rest/sync.py onto sqlite3 + the plugin API.

Routes (all under /plugins/sync/, Basic Auth enforced by the core server):
  POST /plugins/sync/push     uuid-idempotent row upload (phone-authored kinds)
  POST /plugins/sync/pull     opaque-cursor download (interpretive kinds)
  GET  /plugins/sync/status   counts + newest per pullable kind

Where rows live:
  notes                  -> cortex.db (the live core store)
  human_journal_entries  -> plugins/overseer/data/overseer.db
  pulls (gists, temporal narratives) read from overseer.db
  uuid -> remote_id map  -> plugins/sync/data/sync.db (plugin-owned)

local_* timestamp columns are filled by the slice 9.4.1 triggers on insert;
this plugin only writes the canonical UTC created_at the phone sends.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

# plugin_api lives in src/ relative to cortex-core root
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from plugin_api import Plugin, Route  # noqa: E402

log = logging.getLogger("plugin.sync")

OVERSEER_DB = _HERE.parent / "overseer" / "data" / "overseer.db"

# kind -> (cursor prefix, pull columns); identical to the Gateway's table
PULL_KINDS = {
    "summaries_gist": ("g", ["id", "period_label", "body", "confidence", "created_at"]),
    "temporal_narratives": ("nar", ["id", "kind", "period_label", "period_start",
                                    "period_end", "narrative", "created_at"]),
}

# kind -> (target db, insertable columns); phone-authored, append-only
PUSH_KINDS = {
    "human_journal_entries": ("overseer", ["text", "entry_type", "created_at"]),
    "notes": ("core", ["content", "note_type", "project", "tags", "created_at"]),
}


class SyncPlugin(Plugin):
    """Stateless request handlers + a tiny plugin-owned uuid map db."""

    name = "sync"

    def on_load(self):
        self._sync_db_path = Path(self.api.plugin_data) / "sync.db"
        self._sync_db_path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect(self._sync_db_path)
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS sync_row_map (
                       uuid TEXT PRIMARY KEY,
                       kind TEXT NOT NULL,
                       device TEXT DEFAULT '',
                       remote_id INTEGER NOT NULL,
                       created_at TEXT NOT NULL DEFAULT (datetime('now'))
                   )""")
            con.commit()
        finally:
            con.close()
        log.info("sync plugin loaded (core=%s, overseer=%s)",
                 self._core_db_path(), OVERSEER_DB)

    # -- db plumbing --

    def _core_db_path(self):
        p = getattr(self.api, "core_db_path", None)
        if p:
            return str(p)
        from config import CORTEX_DB_PATH  # src/ is on sys.path
        return CORTEX_DB_PATH

    @staticmethod
    def _connect(path):
        con = sqlite3.connect(str(path), timeout=5.0)
        con.row_factory = sqlite3.Row
        return con

    def _target_db(self, which):
        return self._core_db_path() if which == "core" else OVERSEER_DB

    # -- routes --

    def http_routes(self):
        return [
            Route("POST", "/push", self._http_push),
            Route("POST", "/pull", self._http_pull),
            Route("GET", "/status", self._http_status),
        ]

    def _http_push(self, payload):
        kind = str(payload.get("kind") or "")
        spec = PUSH_KINDS.get(kind)
        if spec is None:
            return {"ok": False, "error": "unknown push kind: {}".format(kind)}
        which, cols = spec
        device = str(payload.get("device") or "")
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            return {"ok": False, "error": "rows must be a list"}

        accepted, dupes, rejected, ids = 0, 0, [], {}
        map_con = self._connect(self._sync_db_path)
        tgt_con = self._connect(self._target_db(which))
        try:
            for row in rows:
                if not isinstance(row, dict):
                    rejected.append({"id": None, "reason": "row is not an object"})
                    continue
                uid = row.get("id")
                if not uid or not isinstance(uid, str):
                    rejected.append({"id": uid, "reason": "missing uuid id"})
                    continue
                hit = map_con.execute(
                    "SELECT remote_id FROM sync_row_map WHERE uuid = ?",
                    (uid,)).fetchone()
                if hit:
                    dupes += 1
                    ids[uid] = hit["remote_id"]
                    continue
                values = {c: row.get(c) for c in cols if row.get(c) is not None}
                if kind == "notes":
                    values.setdefault("source", "mobile")
                if not values:
                    rejected.append({"id": uid, "reason": "no insertable columns"})
                    continue
                try:
                    cur = tgt_con.execute(
                        "INSERT INTO {} ({}) VALUES ({})".format(
                            kind, ", ".join(values), ", ".join("?" * len(values))),
                        tuple(values.values()))
                    tgt_con.commit()
                    remote_id = cur.lastrowid
                except Exception as e:
                    rejected.append({"id": uid, "reason": str(e)[:200]})
                    continue
                map_con.execute(
                    "INSERT OR REPLACE INTO sync_row_map "
                    "(uuid, kind, device, remote_id) VALUES (?,?,?,?)",
                    (uid, kind, device, remote_id))
                map_con.commit()
                ids[uid] = remote_id
                accepted += 1
        finally:
            map_con.close()
            tgt_con.close()
        return {"ok": True, "kind": kind, "accepted": accepted,
                "dupes": dupes, "rejected": rejected, "ids": ids}

    def _http_pull(self, payload):
        kind = str(payload.get("kind") or "")
        spec = PULL_KINDS.get(kind)
        if spec is None:
            return {"ok": False, "error": "unknown pull kind: {}".format(kind)}
        prefix, cols = spec
        cursor = str(payload.get("cursor") or "")
        last_id = 0
        if cursor.startswith(prefix + ":"):
            try:
                last_id = int(cursor.split(":", 1)[1])
            except ValueError:
                last_id = 0
        try:
            limit = int(payload.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 100))

        con = self._connect(OVERSEER_DB)
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT {} FROM {} WHERE id > ? ORDER BY id LIMIT ?".format(
                    ", ".join(cols), kind),
                (last_id, limit))]
            more = False
            if rows:
                more = con.execute(
                    "SELECT 1 FROM {} WHERE id > ?".format(kind),
                    (rows[-1]["id"],)).fetchone() is not None
        finally:
            con.close()
        next_cursor = "{}:{}".format(prefix, rows[-1]["id"]) if rows else cursor
        return {"ok": True, "kind": kind, "rows": rows, "more": more,
                "next_cursor": next_cursor}

    def _http_status(self, payload):
        counts, newest = {}, {}
        con = self._connect(OVERSEER_DB)
        try:
            for kind in PULL_KINDS:
                counts[kind] = con.execute(
                    "SELECT count(*) AS c FROM {}".format(kind)).fetchone()["c"]
                row = con.execute(
                    "SELECT created_at FROM {} ORDER BY id DESC LIMIT 1".format(
                        kind)).fetchone()
                newest[kind] = str(row["created_at"]) if row else None
        finally:
            con.close()
        return {"ok": True, "counts": counts, "newest": newest}


def register(api):
    return SyncPlugin(api)
