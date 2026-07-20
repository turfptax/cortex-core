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
import os
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

# Cloud migration P0 (2026-07-20): OVERSEER_DB_PATH env overrides the
# in-tree default, matching the overseer plugin's own resolution, so
# both plugins agree on which overseer.db is live when the cloud
# relocates it. Unset = plugins/overseer/data/overseer.db, unchanged.
_env_overseer_db = os.environ.get("OVERSEER_DB_PATH", "").strip()
OVERSEER_DB = Path(_env_overseer_db) if _env_overseer_db \
    else _HERE.parent / "overseer" / "data" / "overseer.db"

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
    # Device notification capture (2026-06-12): the phone's notification
    # stream as a passive dataset source. Lands in overseer.db; table is
    # created on plugin load.
    "device_notifications": ("overseer", ["app", "title", "body",
                                          "posted_at", "created_at"]),
    # Person-notes dictated on the phone (2026-06-13): a voice note scoped
    # to a synced contact. Lands in overseer.db person_notes. The phone
    # sends person_id (the SERVER id it received from the overseer_people
    # pull) + body; provenance defaults to tory-voice + created_by_agent
    # to mobile (see _http_push). note_kind / modality optional (DB
    # defaults apply). local_created_at: phone is authority on its own tz.
    "person_notes": ("overseer", ["person_id", "body", "note_kind",
                                  "modality", "provenance", "created_at",
                                  "local_created_at", "created_by_agent"]),
    # Voice-assistant transcripts (2026-07-02): the phone's Voice tab pushes
    # each conversation turn as an insert-only row. After accepting rows,
    # _http_push assembles the touched chats into imported_sessions (.jsonl
    # in the overseer's imports dir, source='mobile-voice') so the loop
    # gists them like any AI session.
    "voice_chat_turns": ("overseer", ["chat_id", "chat_title", "role",
                                      "content", "model", "created_at"]),
    # Work-log entries (2026-07-02): the phone voice assistant's log_time
    # tool. Lands in the core time_entries table (columns match 1:1).
    "time_entries": ("core", ["project_tag", "org_tag", "activity_type",
                              "description", "started_at",
                              "duration_minutes", "created_at"]),
    # Bell answers from the phone (2026-07-10): responses to overseer
    # notification action buttons. Same shape the Hub's respond route
    # writes; the overseer picks them up via its existing
    # list_pending_notification_responses tool. A post-accept hook in
    # _http_push archives/dismisses the notification, mirroring the
    # Hub's also_archive default.
    "notification_responses": ("overseer", ["notification_id",
                                            "action_kind", "action_label",
                                            "response_payload_json",
                                            "taken_at"]),
    # Interaction meta-feedback from the phone (2026-07-11): ratings +
    # notes on AI interactions (voice chats, bell conversations). Same
    # table the Hub's /feedback route writes; note-first per Tory's
    # directive. The overseer reads it via get_recent_feedback.
    "interaction_feedback": ("overseer", ["target_kind", "target_id",
                                          "rating", "note",
                                          "context_json", "source",
                                          "created_at"]),
}

DEVICE_NOTIFICATIONS_DDL = """CREATE TABLE IF NOT EXISTS device_notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app TEXT NOT NULL,
  title TEXT DEFAULT '',
  body TEXT DEFAULT '',
  posted_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

VOICE_CHAT_TURNS_DDL = """CREATE TABLE IF NOT EXISTS voice_chat_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  chat_title TEXT DEFAULT '',
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  model TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""


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
        ocon = self._connect(OVERSEER_DB)
        try:
            ocon.execute(DEVICE_NOTIFICATIONS_DDL)
            ocon.execute(VOICE_CHAT_TURNS_DDL)
            ocon.execute(
                "CREATE INDEX IF NOT EXISTS idx_voice_turns_chat "
                "ON voice_chat_turns(chat_id)")
            ocon.commit()
        finally:
            ocon.close()
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
                if kind in ("notes", "time_entries"):
                    values.setdefault("source", "mobile")
                if kind == "person_notes":
                    # Phone-authored = spoken by Tory unless the row says
                    # otherwise (his consent ruling). Stamp authorship too.
                    values.setdefault("provenance", "tory-voice")
                    values.setdefault("created_by_agent", "mobile")
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
                # Voice-created projects reach the core as time entries; make
                # sure the project row exists so the tag resolves everywhere
                # (Hub, MCP, credit math). Stub is cheap and idempotent.
                if kind == "time_entries" and values.get("project_tag"):
                    try:
                        self._ensure_project_stub(
                            tgt_con, str(values["project_tag"]))
                    except Exception as e:
                        log.warning("project stub for %s failed: %s",
                                    values.get("project_tag"), e)
                # Bell answers: settle the notification the same way the
                # Hub's respond route does (dismiss for a plain dismiss,
                # archive for a real answer) so it leaves every surface.
                if kind == "notification_responses" \
                        and values.get("notification_id"):
                    try:
                        col = ("dismissed_at"
                               if values.get("action_kind") == "dismiss"
                               else "archived_at")
                        tgt_con.execute(
                            "UPDATE notifications SET {} = datetime('now') "
                            "WHERE id = ? AND {} IS NULL".format(col, col),
                            (int(values["notification_id"]),))
                        tgt_con.commit()
                    except Exception as e:
                        log.warning("bell settle for %s failed: %s",
                                    values.get("notification_id"), e)
            # Voice transcripts: reconciliation sweep (2026-07-02 review).
            # Assemble EVERY chat, not just the ones this push touched: an
            # assembly that failed on a prior push never retries otherwise,
            # because the phone marks rows synced on the ack and the retry
            # push is all-dupes. Unchanged chats short-circuit on file_hash.
            if kind == "voice_chat_turns":
                chat_rows = tgt_con.execute(
                    "SELECT DISTINCT chat_id FROM voice_chat_turns").fetchall()
                for r in chat_rows:
                    cid = str(r["chat_id"])
                    try:
                        self._assemble_voice_session(tgt_con, cid)
                    except Exception as e:
                        log.warning("voice session assembly failed for %s: %s",
                                    cid, e)
        finally:
            map_con.close()
            tgt_con.close()
        return {"ok": True, "kind": kind, "accepted": accepted,
                "dupes": dupes, "rejected": rejected, "ids": ids}

    @staticmethod
    def _ensure_project_stub(core_con, tag):
        """Create a minimal project row for a tag the phone logs time on but
        the core has never seen (voice-created projects live phone-side only;
        this makes them materialize here the moment work lands on them)."""
        tag = tag.strip()
        if not tag:
            return
        hit = core_con.execute(
            "SELECT 1 FROM projects WHERE tag = ?", (tag,)).fetchone()
        if hit:
            return
        name = tag.replace("-", " ").replace("_", " ").title()
        core_con.execute(
            "INSERT INTO projects (tag, name, status, description) "
            "VALUES (?,?, 'active', 'Auto-created from a mobile time entry')",
            (tag, name))
        core_con.commit()
        log.info("auto-created project stub %s from mobile time entry", tag)

    def _assemble_voice_session(self, ocon, chat_id):
        """Rebuild one voice chat as a Claude-Code-shaped .jsonl + an
        imported_sessions row so the overseer loop gists it like any AI
        session. Re-pushes that grow an already-gisted chat clear its
        processed mark so the next tick re-gists the fuller transcript.
        """
        import hashlib

        turns = ocon.execute(
            "SELECT chat_title, role, content, model, created_at "
            "FROM voice_chat_turns WHERE chat_id = ? ORDER BY created_at, id",
            (chat_id,)).fetchall()
        if not turns:
            return
        title = next((t["chat_title"] for t in turns if t["chat_title"]), "")
        models = sorted({t["model"] for t in turns if t["model"]})

        imports_dir = OVERSEER_DB.parent / "imports" / "mobile-voice"
        imports_dir.mkdir(parents=True, exist_ok=True)
        dest = imports_dir / "{}.jsonl".format(chat_id)
        lines = []
        for t in turns:
            ts = str(t["created_at"] or "").replace(" ", "T")
            lines.append(json.dumps({
                "type": t["role"],
                "sessionId": chat_id,
                "timestamp": ts,
                "message": {"role": t["role"], "content": t["content"]},
            }, ensure_ascii=False))
        blob = ("\n".join(lines) + "\n").encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()

        imp_id = "mobile-voice:{}".format(chat_id)
        prev = ocon.execute(
            "SELECT file_hash FROM imported_sessions WHERE id = ?",
            (imp_id,)).fetchone()
        # Reconciliation-sweep fast path: nothing changed, nothing to do.
        if prev is not None and prev["file_hash"] == digest and dest.exists():
            return
        dest.write_bytes(blob)
        user_n = sum(1 for t in turns if t["role"] == "user")
        asst_n = sum(1 for t in turns if t["role"] == "assistant")
        started = str(turns[0]["created_at"] or "").replace(" ", "T")
        ended = str(turns[-1]["created_at"] or "").replace(" ", "T")
        meta = json.dumps({"title": title, "models": models,
                           "origin": "cortex-mobile voice tab"})
        if prev is None:
            ocon.execute(
                "INSERT INTO imported_sessions (id, source, source_path, "
                "project, started_at, ended_at, message_count, "
                "user_message_count, assistant_message_count, bytes_size, "
                "file_hash, metadata_json, sensitivity) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (imp_id, "mobile-voice", str(dest), "mobile-voice",
                 started, ended, len(turns), user_n, asst_n, len(blob),
                 digest, meta, "internal"))
        elif prev["file_hash"] != digest:
            ocon.execute(
                "UPDATE imported_sessions SET source_path = ?, ended_at = ?, "
                "message_count = ?, user_message_count = ?, "
                "assistant_message_count = ?, bytes_size = ?, file_hash = ?, "
                "metadata_json = ? WHERE id = ?",
                (str(dest), ended, len(turns), user_n, asst_n, len(blob),
                 digest, meta, imp_id))
            # The transcript grew after gisting: clear the processed mark so
            # the loop re-gists the fuller conversation.
            ocon.execute(
                "DELETE FROM processed_imported_sessions WHERE imported_id = ?",
                (imp_id,))
        ocon.commit()

    def _http_pull(self, payload):
        kind = str(payload.get("kind") or "")
        if kind == "gist_nature":
            return self._pull_gist_nature(payload)
        if kind == "gist_vectors":
            return self._pull_gist_vectors(payload)
        if kind == "overseer_people":
            return self._pull_contacts(payload)
        if kind == "bell_notifications":
            return self._pull_bell(payload)
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

    def _pull_contacts(self, payload):
        """Virtual pull kind: canonical contacts (overseer_people), LIVE
        rows only (merged/archived dupes excluded server-side). JSON cols
        are parsed to arrays so the phone gets clean aliases/tags. Cursor
        'person:<id>'. Phone-facing over the DIRECT Pi LAN transport only:
        contacts are NOT in any cloud/Gateway kind, and the BLE bridge
        terminates at the Gateway, so it never carries them either (Slice 13
        PII posture; there is deliberately no off-LAN fallback)."""
        def _arr(s):
            try:
                return json.loads(s or "[]")
            except Exception:
                return []
        last_id = self._cursor_id(payload, "person")
        limit = self._limit(payload)
        con = self._connect(OVERSEER_DB)
        try:
            raw = con.execute(
                "SELECT id, name, display_name, aliases_json, tags_json, "
                "notes, last_interacted_at, created_at "
                "FROM overseer_people "
                "WHERE merged_into_id IS NULL AND id > ? "
                "ORDER BY id LIMIT ?",
                (last_id, limit)).fetchall()
            rows = []
            for r in raw:
                d = dict(r)
                d["aliases"] = _arr(d.pop("aliases_json", "[]"))
                d["tags"] = _arr(d.pop("tags_json", "[]"))
                rows.append(d)
            more = False
            if rows:
                more = con.execute(
                    "SELECT 1 FROM overseer_people "
                    "WHERE merged_into_id IS NULL AND id > ?",
                    (rows[-1]["id"],)).fetchone() is not None
        finally:
            con.close()
        next_cursor = "person:{}".format(rows[-1]["id"]) if rows \
            else str(payload.get("cursor") or "")
        return {"ok": True, "kind": "overseer_people", "rows": rows,
                "more": more, "next_cursor": next_cursor}

    def _pull_bell(self, payload):
        """Virtual pull kind (2026-07-10): the phone-worthy Bell set as a
        SNAPSHOT, not a cursor feed. Notifications are mutable (dismissed
        or archived on the Hub, auto-resolved by rules), so the insert-only
        cursor contract cannot represent them; the phone wipes its local
        copy and re-inserts each pull. Server-side filter keeps the payload
        the ANSWERABLE set: undismissed, unarchived, unsnoozed rows that
        either carry action buttons or are warn/important severity. The
        mission_proposal info flood stays off the phone by design. Pi-only
        posture: bodies can quote corpus content verbatim."""
        limit = self._limit(payload, default=25, cap=50)
        con = self._connect(OVERSEER_DB)
        try:
            raw = con.execute(
                "SELECT id, severity, title, body, rule_name, "
                "       actions_json, created_at "
                "FROM notifications "
                "WHERE dismissed_at IS NULL AND archived_at IS NULL "
                "  AND (snoozed_until IS NULL "
                "       OR snoozed_until <= datetime('now')) "
                "  AND (COALESCE(actions_json, '[]') != '[]' "
                "       OR severity IN ('warn', 'important')) "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
            rows = [dict(r) for r in raw]
        finally:
            con.close()
        # Snapshot semantics: no cursor movement, never more pages.
        return {"ok": True, "kind": "bell_notifications", "rows": rows,
                "more": False,
                "next_cursor": str(payload.get("cursor") or "")}

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
