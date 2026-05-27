"""Cortex Core — Protocol handler and chunk reassembly.

Processes CMD:<command>:<json> messages, dispatches to CortexDB,
and returns RSP:/ACK:/ERR: responses.

Handles CHUNK:n/N:data reassembly for messages exceeding BLE MTU.
"""

import json
import os
import socket
import subprocess
import time

from cortex_db import CortexDB
from config import NOTES_DIR
from note_utils import save_note


class ChunkAssembler:
    """Reassembles CHUNK:n/N:data messages into complete messages."""

    def __init__(self, timeout_s=30.0):
        self._chunks = []
        self._expected = 0
        self._received = set()
        self._started = 0.0
        self._timeout = timeout_s

    @staticmethod
    def is_chunk(msg):
        return msg.startswith("CHUNK:")

    def feed(self, raw_msg):
        """Feed a CHUNK: message. Returns reassembled string when complete,
        None if still accumulating."""
        # Parse CHUNK:n/N:data
        prefix = raw_msg[6:]  # strip "CHUNK:"
        try:
            slash_idx = prefix.index("/")
            seq = int(prefix[:slash_idx])
            rest = prefix[slash_idx + 1:]
            colon_idx = rest.index(":")
            total = int(rest[:colon_idx])
            data = rest[colon_idx + 1:]
        except (ValueError, IndexError):
            self.reset()
            return None

        now = time.monotonic()

        # New sequence or timeout — reset
        if total != self._expected or (
            self._started and now - self._started > self._timeout
        ):
            self.reset()

        if not self._chunks:
            self._chunks = [None] * total
            self._expected = total
            self._started = now

        if 1 <= seq <= total:
            self._chunks[seq - 1] = data
            self._received.add(seq)

        if len(self._received) == self._expected:
            assembled = "".join(self._chunks)
            self.reset()
            return assembled

        return None

    def reset(self):
        self._chunks = []
        self._expected = 0
        self._received = set()
        self._started = 0.0


class CortexProtocol:
    """Processes Cortex CMD: protocol messages and returns responses."""

    def __init__(self, db, plugin_registry=None):
        # Slice 2c2c1: pet + heartbeat refs removed from core. Pet was
        # later extracted entirely to the cortex-pet sister repo
        # (Slice 11, 2026-05-09); plugin is still loaded at runtime on
        # .25 from the sibling repo and serves /plugins/pet/* HTTP routes.
        # Slice 3d-A: plugin_registry is optional; if present, plugins
        # that implement contribute_to_context() get to extend the
        # /api/cmd get_context response (overseer injects working_memory).
        self._db = db
        self._plugin_registry = plugin_registry
        self._assembler = ChunkAssembler()
        self._active_session_id = None

    def handle_message(self, raw_msg, context=None):
        """Process a raw message (CMD: or CHUNK:).

        Args:
            raw_msg: The message string.
            context: Optional dict with runtime state (app_state, uptime_s, etc.)

        Returns:
            Response string to send back, or None if still accumulating chunks.
        """
        # Chunk reassembly
        if ChunkAssembler.is_chunk(raw_msg):
            assembled = self._assembler.feed(raw_msg)
            if assembled is None:
                return None
            raw_msg = assembled

        if not raw_msg.startswith("CMD:"):
            return None

        # Parse CMD:<command>:<payload>
        rest = raw_msg[4:]
        colon_idx = rest.find(":")
        if colon_idx == -1:
            cmd = rest.strip().lower()
            payload = ""
        else:
            cmd = rest[:colon_idx].strip().lower()
            payload = rest[colon_idx + 1:]

        return self._dispatch(cmd, payload, context or {})

    def get_active_session_id(self):
        return self._active_session_id

    def chunk_response(self, msg, max_size=480):
        """Split a response into CHUNK:n/N:data messages if needed."""
        encoded = msg.encode("utf-8")
        if len(encoded) <= max_size:
            return [msg]

        header_reserve = 16  # "CHUNK:nn/nn:" max
        chunk_data_size = max_size - header_reserve
        parts = []
        for i in range(0, len(encoded), chunk_data_size):
            part = encoded[i:i + chunk_data_size].decode("utf-8", errors="replace")
            parts.append(part)

        total = len(parts)
        return [f"CHUNK:{i + 1}/{total}:{part}" for i, part in enumerate(parts)]

    # --- Dispatch ---

    def _dispatch(self, cmd, payload, context):
        handlers = {
            "ping": self._cmd_ping,
            "status": self._cmd_status,
            "note": self._cmd_note,
            "activity": self._cmd_activity,
            "search": self._cmd_search,
            "session_start": self._cmd_session_start,
            "session_end": self._cmd_session_end,
            "get_context": self._cmd_get_context,
            "project_upsert": self._cmd_project_upsert,
            "computer_reg": self._cmd_computer_reg,
            "people_upsert": self._cmd_people_upsert,
            "log_time": self._cmd_log_time,
            "query": self._cmd_query,
            "file_register": self._cmd_file_register,
            "file_list": self._cmd_file_list,
            "file_search": self._cmd_file_search,
            "file_delete": self._cmd_file_delete,
            "wifi_scan": self._cmd_wifi_scan,
            "wifi_config": self._cmd_wifi_config,
            "wifi_status": self._cmd_wifi_status,
            # Pet/heartbeat/dream commands moved out of core in Slice 2c2c1
            # and the pet plugin itself was extracted to the cortex-pet
            # sister repo in Slice 11. Plugin is loaded on .25 and serves
            # /plugins/pet/* HTTP routes from the sibling repo.
            # Training examples & ledger
            "training_upload": self._cmd_training_upload,
            "training_examples": self._cmd_training_examples,
            "training_ledger_get": self._cmd_training_ledger_get,
            "training_ledger_set": self._cmd_training_ledger_set,
            "training_stats": self._cmd_training_stats,
            "training_delete": self._cmd_training_delete,
            "training_update": self._cmd_training_update,
            # Generic CRUD
            "upsert": self._cmd_upsert,
            "delete": self._cmd_delete,
            "table_counts": self._cmd_table_counts,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return f"ERR:{cmd}:unknown command"
        try:
            return handler(payload, context)
        except Exception as e:
            return f"ERR:{cmd}:{e}"

    # --- Command handlers ---

    def _cmd_ping(self, payload, context):
        return "RSP:pong"

    def _cmd_status(self, payload, context):
        stats = self._db.get_stats()
        status = {
            "app_state": context.get("app_state", "unknown"),
            "uptime_s": context.get("uptime_s", 0),
            "disk_free_gb": context.get("disk_free_gb", 0),
            "ble_connected": context.get("ble_connected", False),
            "active_session": self._active_session_id,
        }
        # Include battery info if available
        battery = context.get("battery")
        if battery:
            status["battery"] = battery
        status.update(stats)
        return "RSP:status:" + json.dumps(status, separators=(",", ":"))

    def _cmd_note(self, payload, context):
        data = json.loads(payload) if payload else {}
        content = data.get("content", "")
        if not content:
            return "ERR:note:missing content field"
        _file_path, row_id = save_note(
            content, NOTES_DIR, self._db,
            source="ble",
            note_type=data.get("type", "note"),
            session_id=self._active_session_id,
            tags=data.get("tags", ""),
            project=data.get("project", ""),
        )
        return f"ACK:note:{row_id}"

    def _cmd_activity(self, payload, context):
        data = json.loads(payload) if payload else {}
        program = data.get("program", "")
        if not program:
            return "ERR:activity:missing program field"
        row_id = self._db.insert_activity(
            program=program,
            details=data.get("details", ""),
            file_path=data.get("file_path", ""),
            project=data.get("project", ""),
            session_id=self._active_session_id,
            duration_min=data.get("duration_min", 0),
        )
        return f"ACK:activity:{row_id}"

    def _cmd_log_time(self, payload, context):
        """Log a time entry with auto-project creation."""
        data = json.loads(payload) if payload else {}
        project_tag = data.get("project", "") or data.get("project_tag", "")
        if not project_tag:
            return "ERR:log_time:missing project field"

        duration = data.get("duration_minutes", 0)
        if not duration or duration <= 0:
            return "ERR:log_time:duration_minutes must be > 0"

        # Auto-create project if it doesn't exist
        project_created = False
        if not self._db.project_exists(project_tag):
            self._db.upsert_project(
                tag=project_tag,
                name=data.get("project_name", project_tag),
                status="active",
                description=data.get("project_description", ""),
                category=data.get("category", ""),
                org_tag=data.get("org_tag", ""),
            )
            project_created = True

        # Insert time entry
        row_id = self._db.insert_time_entry(
            project_tag=project_tag,
            org_tag=data.get("org_tag", ""),
            activity_type=data.get("activity_type", "development"),
            description=data.get("description", ""),
            started_at=data.get("date") or data.get("started_at"),
            duration_minutes=duration,
            source="mcp",
        )

        # Recalculate project hours
        self._db.update_project_hours(project_tag)

        # Get updated total
        summary = self._db.get_time_summary(project_tag=project_tag)
        total_hours = sum(r.get("total_hours", 0) for r in summary)

        result = {
            "id": row_id,
            "project": project_tag,
            "project_created": project_created,
            "duration_minutes": duration,
            "total_project_hours": total_hours,
        }
        return "RSP:log_time:" + json.dumps(result, separators=(",", ":"))

    def _cmd_search(self, payload, context):
        data = json.loads(payload) if payload else {}
        query = data.get("query", "")
        if not query:
            return "ERR:search:missing query field"
        row_id = self._db.insert_search(
            query=query,
            source=data.get("source", ""),
            url=data.get("url", ""),
            project=data.get("project", ""),
            session_id=self._active_session_id,
        )
        return f"ACK:search:{row_id}"

    def _cmd_session_start(self, payload, context):
        data = json.loads(payload) if payload else {}
        session_id = self._db.start_session(
            ai_platform=data.get("ai_platform", ""),
            hostname=data.get("hostname", ""),
            os_info=data.get("os_info", ""),
        )
        self._active_session_id = session_id
        return f"ACK:session:{session_id}"

    def _cmd_session_end(self, payload, context):
        data = json.loads(payload) if payload else {}
        session_id = data.get("session_id", self._active_session_id)
        if not session_id:
            return "ERR:session_end:no active session"
        ok = self._db.end_session(
            session_id=session_id,
            summary=data.get("summary", ""),
            projects=data.get("projects", ""),
        )
        if session_id == self._active_session_id:
            self._active_session_id = None
        if ok:
            return f"ACK:session_end:{session_id}"
        return f"ERR:session_end:session not found or already ended"

    def _cmd_get_context(self, payload, context):
        ctx = self._db.get_context()
        # Slice 3d-A: let loaded plugins inject top-level fields. Each
        # plugin's contribute_to_context() returns a dict that is merged
        # into ctx — so the overseer's {"working_memory": {...}, ...}
        # surfaces alongside active_projects, recent_notes, etc. A bad
        # plugin must not break get_context for the user; errors are
        # captured under ctx["_plugin_errors"] for diagnostic surfacing.
        if self._plugin_registry is not None:
            for plugin in getattr(self._plugin_registry, "plugins", []):
                try:
                    contrib = plugin.contribute_to_context()
                except Exception as e:
                    ctx.setdefault("_plugin_errors", {})[
                        getattr(plugin, "name", "")
                        or plugin.__class__.__name__
                    ] = str(e)[:200]
                    continue
                if isinstance(contrib, dict) and contrib:
                    ctx.update(contrib)
        return "RSP:context:" + json.dumps(ctx, separators=(",", ":"), default=str)

    def _cmd_project_upsert(self, payload, context):
        data = json.loads(payload) if payload else {}
        tag = data.get("tag", "")
        if not tag:
            return "ERR:project_upsert:missing tag field"
        self._db.upsert_project(
            tag=tag,
            name=data.get("name", ""),
            status=data.get("status", "active"),
            priority=data.get("priority", 3),
            description=data.get("description", ""),
            category=data.get("category", ""),
            org_tag=data.get("org_tag", ""),
            github_url=data.get("github_url", ""),
            total_hours=data.get("total_hours", 0),
            collaborators=data.get("collaborators", ""),
        )
        return f"ACK:project:{tag}"

    def _cmd_computer_reg(self, payload, context):
        data = json.loads(payload) if payload else {}
        hostname = data.get("hostname", "")
        if not hostname:
            return "ERR:computer_reg:missing hostname field"
        self._db.register_computer(
            hostname=hostname,
            os=data.get("os", ""),
            cpu=data.get("cpu", ""),
            gpu=data.get("gpu", ""),
            ram_gb=data.get("ram_gb", 0),
            notes=data.get("notes", ""),
        )
        return f"ACK:computer:{hostname}"

    def _cmd_people_upsert(self, payload, context):
        data = json.loads(payload) if payload else {}
        person_id = data.get("id", "")
        if not person_id:
            return "ERR:people_upsert:missing id field"
        self._db.upsert_person(
            person_id=person_id,
            name=data.get("name", ""),
            role=data.get("role", ""),
            email=data.get("email", ""),
            projects=data.get("projects", ""),
            notes=data.get("notes", ""),
        )
        return f"ACK:people:{person_id}"

    def _cmd_query(self, payload, context):
        data = json.loads(payload) if payload else {}
        table = data.get("table", "")
        if table not in ("notes", "activities", "searches", "sessions",
                         "projects", "computers", "people", "files",
                         "organizations", "time_entries",
                         "training_examples", "training_ledger"):
            # NOTE: pet_state/pet_interactions live in the cortex-pet
            # sister repo's pet.db (Slice 2c2d schema move; Slice 11 plugin
            # extraction). Use /plugins/pet/history or /plugins/pet/status
            # instead of CMD:query for pet data.
            return "ERR:query:invalid or missing table"
        filters = data.get("filters", {})
        limit = min(data.get("limit", 20), 100)
        order_by = data.get("order_by", "")

        # Build safe query
        sql = f"SELECT * FROM {table}"
        params = []
        if filters:
            clauses = []
            for col, val in filters.items():
                # Only allow alphanumeric column names
                if not col.isalnum():
                    continue
                clauses.append(f"{col} = ?")
                params.append(val)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)

        # Safe order_by: only allow "column ASC/DESC"
        if order_by:
            parts = order_by.split()
            if len(parts) <= 2 and parts[0].replace("_", "").isalnum():
                direction = parts[1].upper() if len(parts) > 1 else "DESC"
                if direction in ("ASC", "DESC"):
                    sql += f" ORDER BY {parts[0]} {direction}"

        sql += " LIMIT ?"
        params.append(limit)

        rows = self._db._conn.execute(sql, params).fetchall()
        results = [dict(r) for r in rows]
        return "RSP:query:" + json.dumps(results, separators=(",", ":"), default=str)

    # --- Generic CRUD ---

    def _cmd_upsert(self, payload, context):
        data = json.loads(payload) if payload else {}
        table = data.get("table", "")
        row_data = data.get("data", {})
        if not table or not row_data:
            return "ERR:upsert:missing table or data"
        try:
            result = self._db.upsert_row(table, row_data)
            return "RSP:upsert:" + json.dumps(
                {"table": table, "id": result}, separators=(",", ":"), default=str
            )
        except ValueError as e:
            return f"ERR:upsert:{e}"

    def _cmd_delete(self, payload, context):
        data = json.loads(payload) if payload else {}
        table = data.get("table", "")
        row_id = data.get("id")
        if not table or row_id is None:
            return "ERR:delete:missing table or id"
        try:
            count = self._db.delete_row(table, row_id)
            return "RSP:delete:" + json.dumps(
                {"table": table, "id": row_id, "deleted": count},
                separators=(",", ":"), default=str,
            )
        except ValueError as e:
            return f"ERR:delete:{e}"

    def _cmd_table_counts(self, payload, context):
        counts = self._db.get_table_counts()
        return "RSP:table_counts:" + json.dumps(
            counts, separators=(",", ":"), default=str
        )

    # --- File metadata ---

    def _cmd_file_register(self, payload, context):
        data = json.loads(payload) if payload else {}
        filename = data.get("filename", "")
        if not filename:
            return "ERR:file_register:missing filename"
        row_id = self._db.insert_file(
            filename=filename,
            category=data.get("category", "uploads"),
            description=data.get("description", ""),
            tags=data.get("tags", ""),
            project=data.get("project", ""),
            mime_type=data.get("mime_type", ""),
            size_bytes=data.get("size_bytes", 0),
            source=data.get("source", "upload"),
            session_id=self._active_session_id,
        )
        return "ACK:file_register:{}".format(row_id)

    def _cmd_file_list(self, payload, context):
        data = json.loads(payload) if payload else {}
        files = self._db.list_files(
            category=data.get("category"),
            project=data.get("project"),
            limit=min(data.get("limit", 50), 100),
        )
        return "RSP:file_list:" + json.dumps(files, separators=(",", ":"), default=str)

    def _cmd_file_search(self, payload, context):
        data = json.loads(payload) if payload else {}
        query = data.get("query", "")
        if not query:
            return "ERR:file_search:missing query"
        files = self._db.search_files(
            query=query,
            limit=min(data.get("limit", 20), 100),
        )
        return "RSP:file_search:" + json.dumps(files, separators=(",", ":"), default=str)

    def _cmd_file_delete(self, payload, context):
        data = json.loads(payload) if payload else {}
        file_id = data.get("id")
        if not file_id:
            return "ERR:file_delete:missing id"
        ok = self._db.delete_file(file_id)
        if ok:
            return "ACK:file_delete:{}".format(file_id)
        return "ERR:file_delete:not found"

    # --- WiFi provisioning (headless setup via BLE) ---

    @staticmethod
    def _get_local_ip():
        """Get the Pi's local IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return None

    def _cmd_wifi_status(self, payload, context):
        """Return current WiFi connection status."""
        info = {"ip": self._get_local_ip()}

        # Try nmcli for SSID and signal
        try:
            r = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,FREQ", "dev", "wifi"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.strip().split("\n"):
                parts = line.split(":")
                if len(parts) >= 2 and parts[0] == "yes":
                    info["ssid"] = parts[1]
                    if len(parts) >= 3:
                        info["signal"] = int(parts[2])
                    break
        except (FileNotFoundError, Exception):
            # Fallback: try iwgetid
            try:
                r = subprocess.run(
                    ["iwgetid", "-r"], capture_output=True, text=True, timeout=5,
                )
                ssid = r.stdout.strip()
                if ssid:
                    info["ssid"] = ssid
            except Exception:
                pass

        # Get hostname
        try:
            info["hostname"] = socket.gethostname()
        except Exception:
            pass

        return "RSP:wifi_status:" + json.dumps(info, separators=(",", ":"))

    def _cmd_wifi_scan(self, payload, context):
        """Scan for available WiFi networks."""
        networks = []

        # Try nmcli
        try:
            subprocess.run(
                ["nmcli", "dev", "wifi", "rescan"],
                capture_output=True, timeout=10,
            )
            time.sleep(2)
            r = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                capture_output=True, text=True, timeout=10,
            )
            seen = set()
            for line in r.stdout.strip().split("\n"):
                parts = line.split(":")
                if len(parts) >= 2 and parts[0] and parts[0] not in seen:
                    seen.add(parts[0])
                    entry = {"ssid": parts[0]}
                    if len(parts) >= 2:
                        try:
                            entry["signal"] = int(parts[1])
                        except ValueError:
                            pass
                    if len(parts) >= 3:
                        entry["security"] = parts[2]
                    networks.append(entry)
            return "RSP:wifi_scan:" + json.dumps(networks, separators=(",", ":"))
        except FileNotFoundError:
            pass
        except Exception as e:
            return "ERR:wifi_scan:{}".format(e)

        # Fallback: iwlist
        try:
            r = subprocess.run(
                ["sudo", "iwlist", "wlan0", "scan"],
                capture_output=True, text=True, timeout=15,
            )
            import re
            for match in re.finditer(r'ESSID:"([^"]*)"', r.stdout):
                ssid = match.group(1)
                if ssid and ssid not in [n["ssid"] for n in networks]:
                    networks.append({"ssid": ssid})
            return "RSP:wifi_scan:" + json.dumps(networks, separators=(",", ":"))
        except Exception as e:
            return "ERR:wifi_scan:{}".format(e)

    def _cmd_wifi_config(self, payload, context):
        """Connect to a WiFi network (headless provisioning via BLE).

        Payload: {"ssid": "...", "password": "..."}
        """
        data = json.loads(payload) if payload else {}
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        if not ssid:
            return "ERR:wifi_config:missing ssid"

        # Try nmcli (Raspberry Pi OS Bookworm+)
        try:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                time.sleep(2)
                ip = self._get_local_ip()
                return "RSP:wifi_config:" + json.dumps(
                    {"ok": True, "ssid": ssid, "ip": ip}, separators=(",", ":"))
            else:
                return "ERR:wifi_config:{}".format(r.stderr.strip())
        except FileNotFoundError:
            pass
        except Exception as e:
            return "ERR:wifi_config:nmcli failed: {}".format(e)

        # Fallback: wpa_cli
        try:
            def wpa(args):
                return subprocess.run(
                    ["wpa_cli", "-i", "wlan0"] + args,
                    capture_output=True, text=True, timeout=10,
                )
            r = wpa(["add_network"])
            net_id = r.stdout.strip()
            wpa(["set_network", net_id, "ssid", '"{}"'.format(ssid)])
            if password:
                wpa(["set_network", net_id, "psk", '"{}"'.format(password)])
            else:
                wpa(["set_network", net_id, "key_mgmt", "NONE"])
            wpa(["enable_network", net_id])
            wpa(["save_config"])
            time.sleep(3)
            ip = self._get_local_ip()
            return "RSP:wifi_config:" + json.dumps(
                {"ok": True, "ssid": ssid, "ip": ip}, separators=(",", ":"))
        except Exception as e:
            return "ERR:wifi_config:wpa_cli failed: {}".format(e)
    # --- Training Examples & Ledger ---

    def _cmd_training_upload(self, payload, context):
        """Bulk upload training examples.
        Payload: {examples: [{messages, source, source_id, cycle_id, model, server}]}
        """
        data = json.loads(payload) if payload else {}
        examples = data.get("examples", [])
        if not examples:
            return "ERR:training_upload:no examples provided"
        count = self._db.bulk_insert_training_examples(examples)
        return f"ACK:training_upload:{count}"

    def _cmd_training_examples(self, payload, context):
        """Get training examples. Optional filters: cycle_id, source, limit, offset."""
        data = json.loads(payload) if payload else {}
        cycle_id = data.get("cycle_id")
        source = data.get("source")
        limit = min(data.get("limit", 100), 1000)
        offset = data.get("offset", 0)
        examples = self._db.get_training_examples(
            cycle_id=cycle_id, source=source, limit=limit, offset=offset
        )
        total = self._db.count_training_examples(cycle_id=cycle_id, source=source)
        result = {"examples": examples, "total": total, "limit": limit, "offset": offset}
        return "RSP:training_examples:" + json.dumps(
            result, separators=(",", ":"), default=str)

    def _cmd_training_ledger_get(self, payload, context):
        """Get the training ledger (learn cycle state)."""
        ledger = self._db.get_training_ledger()
        return "RSP:training_ledger_get:" + json.dumps(
            ledger, separators=(",", ":"), default=str)

    def _cmd_training_ledger_set(self, payload, context):
        """Update the training ledger. Payload: {key: value, ...}."""
        data = json.loads(payload) if payload else {}
        if not data:
            return "ERR:training_ledger_set:no data provided"
        for key, value in data.items():
            self._db.set_training_ledger(key, value)
        return f"ACK:training_ledger_set:{len(data)}"

    def _cmd_training_stats(self, payload, context):
        """Get training statistics."""
        stats = self._db.get_training_stats()
        return "RSP:training_stats:" + json.dumps(
            stats, separators=(",", ":"), default=str)

    def _cmd_training_delete(self, payload, context):
        """Delete training examples by ID.

        Payload: {"ids": [1, 2, 3]}
        """
        data = json.loads(payload) if payload else {}
        ids = data.get("ids", [])
        if not ids:
            return "ERR:training_delete:missing ids"
        placeholders = ",".join("?" for _ in ids)
        self._db._conn.execute(
            f"DELETE FROM training_examples WHERE id IN ({placeholders})",
            ids,
        )
        self._db._conn.commit()
        return f"ACK:training_delete:{len(ids)}"

    def _cmd_training_update(self, payload, context):
        """Update a training example's messages.

        Payload: {"id": 1, "messages": [...]}
        """
        data = json.loads(payload) if payload else {}
        example_id = data.get("id")
        messages = data.get("messages")
        if not example_id or messages is None:
            return "ERR:training_update:missing id or messages"
        self._db._conn.execute(
            "UPDATE training_examples SET messages = ? WHERE id = ?",
            (json.dumps(messages, ensure_ascii=False), example_id),
        )
        self._db._conn.commit()
        return f"ACK:training_update:{example_id}"

