"""Cortex Core -- HTTP API server (runs in background thread).

Exposes the Cortex protocol over HTTP for direct WiFi access from
computers on the local network. Runs alongside BLE -- both transports
active simultaneously.

Data flow:
    AI Agent -> cortex-mcp -> HTTP (WiFi) -> this server -> CortexProtocol -> SQLite

Endpoints:
    GET  /health                          -> health check (no auth)
    POST /api/cmd                         -> execute CMD: protocol command
    GET  /files/<category>                -> list files
    GET  /files/<category>/<name>         -> download file
    GET  /files/db                        -> download cortex.db snapshot
    POST /files/uploads                   -> upload file (raw body + X-Filename header)
    DELETE /files/<category>/<name>       -> delete file
    *    /plugins/<plugin_name>/<path>    -> mounted plugin route (slice 2c2b)
                                             handler signature:
                                                 (payload: dict) -> dict
"""

import base64
import json
import os
import secrets
import shutil
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from config import (
    HTTP_PORT, HTTP_USERNAME, HTTP_PASSWORD, RECORDING_DIR, NOTES_DIR,
    LOG_DIR, UPLOADS_DIR, CORTEX_DB_PATH,
)

# Directory mapping for file serving
_FILE_DIRS = {
    "recordings": RECORDING_DIR,
    "notes": NOTES_DIR,
    "logs": LOG_DIR,
    "uploads": UPLOADS_DIR,
}

_STREAM_CHUNK = 65536  # 64KB chunks for file streaming (Pi Zero memory-safe)
_MAX_UPLOAD = 100 * 1024 * 1024  # 100MB max upload
_MAX_CMD_BODY = 1024 * 1024  # 1MB max command body


def _check_basic_auth(auth_header, username, password):
    """Validate HTTP Basic Auth header against configured credentials."""
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        req_user, req_pass = decoded.split(":", 1)
        return (secrets.compare_digest(req_user, username) and
                secrets.compare_digest(req_pass, password))
    except Exception:
        return False


def _safe_filename(name):
    """Sanitize filename to prevent directory traversal."""
    base = os.path.basename(name)
    if not base or base.startswith(".") or ".." in base:
        return None
    return base


def _mime_type(filename):
    """Return Content-Type for common file extensions."""
    if filename.endswith(".wav"):
        return "audio/wav"
    if filename.endswith(".txt"):
        return "text/plain; charset=utf-8"
    if filename.endswith(".jsonl") or filename.endswith(".json"):
        return "application/json"
    if filename.endswith(".db"):
        return "application/x-sqlite3"
    return "application/octet-stream"


class CortexHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Cortex WiFi API."""

    # Use HTTP/1.1 for persistent connections
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass

    # -- Auth --

    def _check_auth(self):
        """Validate HTTP Basic Auth (username:password)."""
        auth = self.headers.get("Authorization", "")
        return _check_basic_auth(auth, self.server.username, self.server.password)

    # -- Response helpers --

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        body = json.dumps({"ok": False, "error": message}, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="Cortex"')
        self.end_headers()
        self.wfile.write(body)

    # -- Plugin route helpers --

    def _try_plugin_route(self, method, path):
        """Match path against registered plugin routes. Returns True if served."""
        for route_method, route_path, handler in self.server.plugin_routes:
            if route_method == method and route_path == path:
                self._invoke_plugin_handler(handler)
                return True
        return False

    def _invoke_plugin_handler(self, handler):
        """Build payload from query + body, call handler(payload) -> dict, send JSON.

        Plugin handler contract (slice 2c2c1):
          - input: payload dict, populated from:
              * query string params (each value is a string; handler casts)
              * JSON body (must be an object; overlays query params on conflict)
              * payload["__client_ip__"]: requesting client's IP (for handlers
                that need it, e.g. tuck-in registers Hub IP for dream callbacks)
          - output: dict (server JSON-encodes)
          - exception -> 500 with message
        """
        payload = {}

        # Query string params
        parsed = urlparse(self.path)
        if parsed.query:
            for key, vals in parse_qs(parsed.query).items():
                payload[key] = vals[-1] if len(vals) == 1 else vals

        # JSON body (POST/PUT) overlays query params
        length = int(self.headers.get("Content-Length", 0))
        if length > _MAX_CMD_BODY:
            self._error(413, "Request body too large")
            return
        if length > 0:
            try:
                body = self.rfile.read(length)
                body_data = json.loads(body) if body else {}
                if not isinstance(body_data, dict):
                    self._error(400, "Plugin route body must be a JSON object")
                    return
                payload.update(body_data)
            except (ValueError, json.JSONDecodeError):
                self._error(400, "Invalid JSON body")
                return

        # Runtime metadata: client IP for handlers that need it (tuck-in, etc.)
        payload["__client_ip__"] = self.client_address[0]

        try:
            result = handler(payload)
        except Exception as e:
            self._error(500, "Plugin handler error: {}".format(e))
            return
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        self._json(result)

    # -- Routing --

    def do_GET(self):
        path = unquote(urlparse(self.path).path).rstrip("/")

        if path == "/health":
            self._json({
                "ok": True,
                "uptime_s": round(time.monotonic(), 1),
                "timestamp": datetime.utcnow().isoformat() + "Z",
            })
            return

        if path == "" or path == "/":
            if not self._check_auth():
                self._error(401, "Unauthorized")
                return
            self._serve_index()
            return

        if not self._check_auth():
            self._error(401, "Unauthorized")
            return

        if path.startswith("/plugins/"):
            if self._try_plugin_route("GET", path):
                return
            self._error(404, "Plugin route not found: {}".format(path))
            return

        if path == "/files/db":
            self._serve_db()
            return

        if path.startswith("/files/"):
            self._route_files_get(path)
            return

        self._error(404, "Not found")

    def do_POST(self):
        if not self._check_auth():
            self._error(401, "Unauthorized")
            return

        path = unquote(urlparse(self.path).path).rstrip("/")

        if path == "/api/cmd":
            self._handle_cmd()
            return

        if path.startswith("/plugins/"):
            if self._try_plugin_route("POST", path):
                return
            self._error(404, "Plugin route not found: {}".format(path))
            return

        if path == "/files/uploads":
            self._handle_upload()
            return

        self._error(404, "Not found")

    def do_DELETE(self):
        if not self._check_auth():
            self._error(401, "Unauthorized")
            return

        path = unquote(urlparse(self.path).path).rstrip("/")

        if path.startswith("/plugins/"):
            if self._try_plugin_route("DELETE", path):
                return
            self._error(404, "Plugin route not found: {}".format(path))
            return

        if path.startswith("/files/"):
            self._handle_delete(path)
            return

        self._error(404, "Not found")

    # -- Index page --

    def _serve_index(self):
        """GET / -- simple status page for browser access."""
        uptime = round(time.monotonic(), 1)
        ts = datetime.utcnow().isoformat() + "Z"

        # Gather disk stats
        try:
            st = os.statvfs("/")
            total_gb = (st.f_frsize * st.f_blocks) / (1024 ** 3)
            free_gb = (st.f_frsize * st.f_bavail) / (1024 ** 3)
            disk_info = "{:.1f} GB free / {:.1f} GB total".format(free_gb, total_gb)
        except Exception:
            disk_info = "unknown"

        # Count files per category
        counts = {}
        for cat, dir_path in _FILE_DIRS.items():
            try:
                counts[cat] = len([f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))])
            except Exception:
                counts[cat] = 0

        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Cortex Core</title>
<style>
body {{ font-family: monospace; background: #111; color: #eee; padding: 2em; max-width: 600px; margin: 0 auto; }}
h1 {{ color: #0af; margin-bottom: 0.2em; }}
.sub {{ color: #888; margin-bottom: 2em; }}
.card {{ background: #1a1a2a; border: 1px solid #333; border-radius: 8px; padding: 1em; margin: 1em 0; }}
.card h3 {{ margin: 0 0 0.5em 0; color: #0af; }}
.row {{ display: flex; justify-content: space-between; padding: 0.2em 0; }}
.label {{ color: #888; }}
.ok {{ color: #0c8; }}
a {{ color: #0af; }}
</style></head><body>
<h1>Cortex Core</h1>
<p class="sub">Pi Zero 2W &middot; WiFi API</p>
<div class="card">
  <h3>Status</h3>
  <div class="row"><span class="label">Online</span><span class="ok">Yes</span></div>
  <div class="row"><span class="label">Uptime</span><span>{uptime}s</span></div>
  <div class="row"><span class="label">Time</span><span>{ts}</span></div>
  <div class="row"><span class="label">Disk</span><span>{disk}</span></div>
</div>
<div class="card">
  <h3>Files</h3>
  <div class="row"><span class="label">Recordings</span><span>{rec}</span></div>
  <div class="row"><span class="label">Notes</span><span>{notes}</span></div>
  <div class="row"><span class="label">Uploads</span><span>{uploads}</span></div>
  <div class="row"><span class="label">Logs</span><span>{logs}</span></div>
</div>
<div class="card">
  <h3>API Endpoints</h3>
  <div class="row"><a href="/health">/health</a><span class="label">Health check (no auth)</span></div>
  <div class="row"><a href="/files/recordings">/files/recordings</a><span class="label">List recordings</span></div>
  <div class="row"><a href="/files/notes">/files/notes</a><span class="label">List notes</span></div>
  <div class="row"><a href="/files/uploads">/files/uploads</a><span class="label">List uploads</span></div>
  <div class="row"><a href="/files/logs">/files/logs</a><span class="label">List logs</span></div>
</div>
</body></html>""".format(
            uptime=uptime, ts=ts, disk=disk_info,
            rec=counts.get("recordings", 0),
            notes=counts.get("notes", 0),
            uploads=counts.get("uploads", 0),
            logs=counts.get("logs", 0),
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- Command API --

    def _handle_cmd(self):
        """POST /api/cmd -- execute a Cortex protocol command."""
        length = int(self.headers.get("Content-Length", 0))
        if length > _MAX_CMD_BODY:
            self._error(413, "Request body too large")
            return

        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._error(400, "Invalid JSON body")
            return

        command = body.get("command", "")
        payload = body.get("payload")

        if not command:
            self._error(400, "Missing 'command' field")
            return

        # Build CMD: protocol message (same format as BLE)
        if payload is not None:
            msg = "CMD:{}:{}".format(command, json.dumps(payload))
        else:
            msg = "CMD:{}".format(command)

        # NOTE: heartbeat Hub-IP tracking moved to the pet plugin's tuck-in
        # route in slice 2c2c1 (uses payload["__client_ip__"] — see
        # _invoke_plugin_handler above). cortex_protocol no longer holds
        # a heartbeat reference.

        # Call protocol handler directly -- the same method that processes BLE messages
        protocol = self.server.cortex_protocol
        context = self.server.context_fn() if self.server.context_fn else {}
        response = protocol.handle_message(msg, context=context)

        if response is None:
            # Still accumulating chunks (shouldn't happen over HTTP, but handle gracefully)
            self._json({"ok": True, "response": None})
        else:
            self._json({"ok": True, "response": response})

    # -- File operations --

    def _route_files_get(self, path):
        """Route /files/<category> or /files/<category>/<filename>."""
        parts = path.split("/")
        # /files/<category> -> ["", "files", "<category>"]
        # /files/<category>/<name> -> ["", "files", "<category>", "<name>"]

        if len(parts) == 3:
            self._list_files(parts[2])
        elif len(parts) == 4:
            self._download_file(parts[2], parts[3])
        else:
            self._error(404, "Not found")

    def _list_files(self, category):
        """GET /files/<category> -- list files with name, size, mtime, and DB metadata."""
        dir_path = _FILE_DIRS.get(category)
        if not dir_path:
            self._error(404, "Unknown category: {}".format(category))
            return

        if not os.path.isdir(dir_path):
            self._json({"ok": True, "category": category, "files": []})
            return

        # Get DB metadata for this category (keyed by filename)
        db_meta = {}
        try:
            db = self.server.cortex_protocol._db
            for row in db.list_files(category=category, limit=500):
                db_meta[row["filename"]] = row
        except Exception:
            pass

        files = []
        for name in sorted(os.listdir(dir_path)):
            filepath = os.path.join(dir_path, name)
            if os.path.isfile(filepath):
                st = os.stat(filepath)
                entry = {
                    "name": name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
                }
                meta = db_meta.get(name)
                if meta:
                    entry["description"] = meta.get("description", "")
                    entry["tags"] = meta.get("tags", "")
                    entry["project"] = meta.get("project", "")
                    entry["file_id"] = meta.get("id")
                files.append(entry)

        self._json({"ok": True, "category": category, "files": files})

    def _download_file(self, category, filename):
        """GET /files/<category>/<filename> -- stream file download."""
        dir_path = _FILE_DIRS.get(category)
        if not dir_path:
            self._error(404, "Unknown category")
            return

        safe_name = _safe_filename(filename)
        if not safe_name:
            self._error(400, "Invalid filename")
            return

        filepath = os.path.join(dir_path, safe_name)
        if not os.path.isfile(filepath):
            self._error(404, "File not found")
            return

        file_size = os.path.getsize(filepath)
        self.send_response(200)
        self.send_header("Content-Type", _mime_type(safe_name))
        self.send_header("Content-Length", str(file_size))
        self.send_header("Content-Disposition",
                         'attachment; filename="{}"'.format(safe_name))
        self.end_headers()

        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(_STREAM_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _serve_db(self):
        """GET /files/db -- download cortex.db as a consistent snapshot."""
        if not os.path.isfile(CORTEX_DB_PATH):
            self._error(404, "Database not found")
            return

        # Copy to temp file for consistent read (WAL mode may have uncommitted data)
        tmp_path = CORTEX_DB_PATH + ".download"
        try:
            shutil.copy2(CORTEX_DB_PATH, tmp_path)
            # Copy WAL if present for consistent snapshot
            wal = CORTEX_DB_PATH + "-wal"
            if os.path.exists(wal):
                shutil.copy2(wal, tmp_path + "-wal")

            file_size = os.path.getsize(tmp_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-sqlite3")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Content-Disposition",
                             'attachment; filename="cortex.db"')
            self.end_headers()

            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(_STREAM_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            for p in [tmp_path, tmp_path + "-wal", tmp_path + "-shm"]:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _handle_upload(self):
        """POST /files/uploads -- upload file (raw body + X-Filename header)."""
        filename = self.headers.get("X-Filename", "")
        if not filename:
            self._error(400, "Missing X-Filename header")
            return

        safe_name = _safe_filename(filename)
        if not safe_name:
            self._error(400, "Invalid filename")
            return

        length = int(self.headers.get("Content-Length", 0))
        if length > _MAX_UPLOAD:
            self._error(413, "File too large (max 100MB)")
            return
        if length == 0:
            self._error(400, "Empty body")
            return

        os.makedirs(UPLOADS_DIR, exist_ok=True)
        dest = os.path.join(UPLOADS_DIR, safe_name)

        # Stream to file in chunks (memory-safe)
        remaining = length
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk_size = min(_STREAM_CHUNK, remaining)
                data = self.rfile.read(chunk_size)
                if not data:
                    break
                f.write(data)
                remaining -= len(data)

        # Register file metadata in database
        file_id = None
        try:
            db = self.server.cortex_protocol._db
            file_id = db.insert_file(
                filename=safe_name,
                category="uploads",
                description=self.headers.get("X-Description", ""),
                tags=self.headers.get("X-Tags", ""),
                project=self.headers.get("X-Project", ""),
                mime_type=_mime_type(safe_name),
                size_bytes=length,
                source="http",
            )
        except Exception:
            pass

        self._json({
            "ok": True,
            "filename": safe_name,
            "size": length,
            "path": dest,
            "file_id": file_id,
        })

    def _handle_delete(self, path):
        """DELETE /files/<category>/<filename> -- delete a file."""
        parts = path.split("/")
        if len(parts) != 4:
            self._error(404, "Not found")
            return

        category, filename = parts[2], parts[3]

        # Only allow deleting from recordings and uploads
        if category not in ("recordings", "uploads"):
            self._error(403, "Deletion not allowed for category: {}".format(category))
            return

        dir_path = _FILE_DIRS.get(category)
        if not dir_path:
            self._error(404, "Unknown category")
            return

        safe_name = _safe_filename(filename)
        if not safe_name:
            self._error(400, "Invalid filename")
            return

        filepath = os.path.join(dir_path, safe_name)
        if not os.path.isfile(filepath):
            self._error(404, "File not found")
            return

        os.unlink(filepath)
        self._json({"ok": True, "deleted": safe_name})


class CortexHTTPServer(ThreadingHTTPServer):
    """HTTP server with references to Cortex protocol and Basic Auth credentials.

    plugin_routes is a list of (method, mount_path, handler) tuples handed
    in by main.py from the PluginRegistry. Each handler has signature
    `(payload: dict) -> dict`.
    """

    def __init__(self, addr, handler, cortex_protocol, context_fn, username, password,
                 plugin_routes=None):
        self.cortex_protocol = cortex_protocol
        self.context_fn = context_fn
        self.username = username
        self.password = password
        self.plugin_routes = list(plugin_routes or [])
        super().__init__(addr, handler)


def start_http_server(cortex_protocol, context_fn=None, port=None, plugin_routes=None):
    """Start the HTTP API server in a background daemon thread.

    Args:
        cortex_protocol: CortexProtocol instance (shared with main loop and BLE).
        context_fn: Callable returning runtime context dict (app_state, uptime, etc).
        port: TCP port to bind (default: config.HTTP_PORT).
        plugin_routes: Optional list of (method, mount_path, handler) tuples
                       collected from PluginRegistry.get_http_routes(). Each
                       handler has signature (payload: dict) -> dict and is
                       served under /plugins/<plugin_name>/<route_path>.

    Returns:
        (thread, server) tuple.
    """
    port = port or HTTP_PORT
    os.makedirs(UPLOADS_DIR, exist_ok=True)

    server = CortexHTTPServer(
        ("0.0.0.0", port),
        CortexHTTPHandler,
        cortex_protocol,
        context_fn,
        HTTP_USERNAME,
        HTTP_PASSWORD,
        plugin_routes=plugin_routes,
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="http-api")
    thread.start()
    print("HTTP API server started on port {} (user: {})".format(port, HTTP_USERNAME))
    if plugin_routes:
        print("  mounted {} plugin route(s):".format(len(plugin_routes)))
        for method, mount_path, _ in plugin_routes:
            print("    {} {}".format(method, mount_path))
    return thread, server
