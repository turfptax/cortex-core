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

import json
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

# kind -> (cursor prefix, pull columns). Extended 2026-06-12: the phone
# mirrors the WHOLE interpretive layer, not just gists + narratives.
PULL_KINDS = {
    "summaries_gist": ("g", ["id", "period_label", "body", "confidence", "created_at"]),
    "temporal_narratives": ("nar", ["id", "kind", "period_label", "period_start",
                                    "period_end", "narrative", "created_at"]),
    "summaries_theme": ("t", ["id", "title", "body", "confidence", "created_at"]),
    "summaries_episode": ("e", ["id", "title", "body", "created_at"]),
    # Pi schema realities (2026-06-12): open_questions tracks is_active not
    # status; the journal and future notes stamp written_at. Aliases keep
    # the phone-facing column names uniform.
    "open_questions": ("q", ["id", "question", "body", "confidence",
                             "CASE WHEN is_active = 1 THEN 'open' ELSE 'closed' END AS status",
                             "created_at"]),
    "patterns": ("p", ["id", "name", "body", "confidence", "created_at"]),
    "drift_observations": ("d", ["id", "body", "direction", "created_at"]),
    "overseer_journal": ("j", ["id", "body", "written_at AS created_at"]),
    "known_blindspots": ("b", ["id", "body", "rationale", "created_at"]),
    "future_overseer_notes": ("n", ["id", "body", "written_at AS created_at"]),
}

# Per-gist nature weight, computed from session_nature (the Stage 0.5
# classification) via the period_label tail join. LEFT JOIN: gists without
# a classified session keep weight 1.0.
GIST_NATURE_SQL = """
SELECT g.id AS gist_id,
       COALESCE(sn.category, '') AS category,
       COALESCE(CASE sn.category
           WHEN 'human-dialogue'     THEN 1.0
           WHEN 'human-build'        THEN 0.8
           WHEN 'automation-checkin' THEN 0.2
           WHEN 'automation-batch'   THEN 0.1
       END, 1.0) AS weight
FROM summaries_gist g
LEFT JOIN session_nature sn
       ON substr(sn.session_id, -12) = substr(g.period_label, -12)
WHERE g.id > ? ORDER BY g.id LIMIT ?
"""

EMBED_URL = "http://127.0.0.1:8082/embedding"

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
            Route("POST", "/embed", self._http_embed),
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
        if kind == "gist_nature":
            return self._pull_gist_nature(payload)
        if kind == "gist_vectors":
            return self._pull_gist_vectors(payload)
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

    @staticmethod
    def _cursor_id(payload, prefix):
        cursor = str(payload.get("cursor") or "")
        if cursor.startswith(prefix + ":"):
            try:
                return int(cursor.split(":", 1)[1])
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _limit(payload, default=50, cap=100):
        try:
            n = int(payload.get("limit") or default)
        except (TypeError, ValueError):
            n = default
        return max(1, min(n, cap))

    def _pull_gist_nature(self, payload):
        """Virtual pull kind: per-gist memory weight from session_nature."""
        last_id = self._cursor_id(payload, "gn")
        limit = self._limit(payload)
        con = self._connect(OVERSEER_DB)
        try:
            rows = [dict(r) for r in con.execute(GIST_NATURE_SQL, (last_id, limit))]
            more = False
            if rows:
                more = con.execute(
                    "SELECT 1 FROM summaries_gist WHERE id > ?",
                    (rows[-1]["gist_id"],)).fetchone() is not None
        finally:
            con.close()
        next_cursor = "gn:{}".format(rows[-1]["gist_id"]) if rows \
            else str(payload.get("cursor") or "")
        return {"ok": True, "kind": "gist_nature", "rows": rows,
                "more": more, "next_cursor": next_cursor}

    def _pull_gist_vectors(self, payload):
        """Virtual pull kind: bge-small embeddings from the sqlite-vec table,
        base64-encoded for JSON transport. Needs the sqlite_vec extension."""
        import base64
        last_id = self._cursor_id(payload, "gv")
        limit = self._limit(payload, default=50, cap=50)
        con = self._connect(OVERSEER_DB)
        try:
            try:
                import sqlite_vec
                con.enable_load_extension(True)
                sqlite_vec.load(con)
                con.enable_load_extension(False)
            except Exception as e:
                return {"ok": False,
                        "error": "sqlite-vec unavailable: {}".format(e)}
            raw = con.execute(
                "SELECT gist_id, embedding FROM vec_gists "
                "WHERE gist_id > ? ORDER BY gist_id LIMIT ?",
                (last_id, limit)).fetchall()
            rows = [{"gist_id": r["gist_id"],
                     "dim": len(r["embedding"]) // 4,
                     "vec_b64": base64.b64encode(r["embedding"]).decode()}
                    for r in raw]
            more = False
            if rows:
                more = con.execute(
                    "SELECT 1 FROM vec_gists WHERE gist_id > ?",
                    (rows[-1]["gist_id"],)).fetchone() is not None
        finally:
            con.close()
        next_cursor = "gv:{}".format(rows[-1]["gist_id"]) if rows \
            else str(payload.get("cursor") or "")
        return {"ok": True, "kind": "gist_vectors", "rows": rows,
                "more": more, "next_cursor": next_cursor}

    def _http_embed(self, payload):
        """Embed query text via the local llama-embed service so the phone
        can run semantic KNN over its synced vectors."""
        import urllib.request
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "missing text"}
        req = urllib.request.Request(
            EMBED_URL, data=json.dumps({"content": text[:2000]}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            return {"ok": False, "error": "llama-embed: {}".format(e)}
        # llama.cpp variants: {"embedding": [...]}, [{"embedding": [...]}],
        # or {"data": [{"embedding": [...]}]}.
        emb = None
        if isinstance(data, dict):
            emb = data.get("embedding") or (
                (data.get("data") or [{}])[0].get("embedding")
                if isinstance(data.get("data"), list) else None)
        elif isinstance(data, list) and data:
            emb = data[0].get("embedding")
        if isinstance(emb, list) and emb and isinstance(emb[0], list):
            emb = emb[0]  # some builds nest one level deeper
        if not isinstance(emb, list) or not emb:
            return {"ok": False, "error": "unexpected llama-embed reply shape"}
        return {"ok": True, "embedding": emb, "dim": len(emb)}

    def _http_status(self, payload):
        counts, newest = {}, {}
        con = self._connect(OVERSEER_DB)
        try:
            for kind, (_prefix, cols) in PULL_KINDS.items():
                counts[kind] = con.execute(
                    "SELECT count(*) AS c FROM {}".format(kind)).fetchone()["c"]
                # cols may carry aliases (written_at AS created_at), so select
                # the kind's own column expressions rather than a literal name.
                row = con.execute(
                    "SELECT {} FROM {} ORDER BY id DESC LIMIT 1".format(
                        ", ".join(cols), kind)).fetchone()
                newest[kind] = str(row["created_at"]) if row else None
        finally:
            con.close()
        return {"ok": True, "counts": counts, "newest": newest}


def register(api):
    return SyncPlugin(api)
