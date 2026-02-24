"""Cortex Core -- HTTP API server (runs in background thread).

Exposes the Cortex protocol over HTTP for direct WiFi access from
computers on the local network. Runs alongside BLE -- both transports
active simultaneously.

Data flow:
    AI Agent -> cortex-mcp -> HTTP (WiFi) -> this server -> CortexProtocol -> SQLite

Endpoints:
    GET  /health                  -> health check (no auth)
    POST /api/cmd                 -> execute CMD: protocol command
    GET  /files/<category>        -> list files
    GET  /files/<category>/<name> -> download file
    GET  /files/db                -> download cortex.db snapshot
    POST /files/uploads           -> upload file (raw body + X-Filename header)
    DELETE /files/<category>/<name> -> delete file
"""

import json
import os
import secrets
import shutil
import stat
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from config import (
    HTTP_PORT, HTTP_TOKEN_PATH, RECORDING_DIR, NOTES_DIR,
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


def _load_or_create_token():
    """Load existing bearer token or generate a new one."""
    try:
        with open(HTTP_TOKEN_PATH, "r") as f:
            token = f.read().strip()
            if token:
                return token
    except FileNotFoundError:
        pass

    token = secrets.token_hex(32)
    with open(HTTP_TOKEN_PATH, "w") as f:
        f.write(token + "\n")
    try:
        os.chmod(HTTP_TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)  # 0644
    except OSError:
        pass
    return token


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
        """Validate Authorization: Bearer <token>."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return secrets.compare_digest(auth[7:], self.server.token)
        return False

    # -- Response helpers --

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._json({"ok": False, "error": message}, status=status)

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

        if not self._check_auth():
            self._error(401, "Unauthorized")
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

        if path == "/files/uploads":
            self._handle_upload()
            return

        self._error(404, "Not found")

    def do_DELETE(self):
        if not self._check_auth():
            self._error(401, "Unauthorized")
            return

        path = unquote(urlparse(self.path).path).rstrip("/")
        if path.startswith("/files/"):
            self._handle_delete(path)
            return

        self._error(404, "Not found")

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
        """GET /files/<category> -- list files with name, size, mtime."""
        dir_path = _FILE_DIRS.get(category)
        if not dir_path:
            self._error(404, "Unknown category: {}".format(category))
            return

        if not os.path.isdir(dir_path):
            self._json({"ok": True, "category": category, "files": []})
            return

        files = []
        for name in sorted(os.listdir(dir_path)):
            filepath = os.path.join(dir_path, name)
            if os.path.isfile(filepath):
                st = os.stat(filepath)
                files.append({
                    "name": name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
                })

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

        self._json({
            "ok": True,
            "filename": safe_name,
            "size": length,
            "path": dest,
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
    """HTTP server with references to Cortex protocol and auth token."""

    def __init__(self, addr, handler, cortex_protocol, context_fn, token):
        self.cortex_protocol = cortex_protocol
        self.context_fn = context_fn
        self.token = token
        super().__init__(addr, handler)


def start_http_server(cortex_protocol, context_fn=None, port=None):
    """Start the HTTP API server in a background daemon thread.

    Args:
        cortex_protocol: CortexProtocol instance (shared with main loop and BLE).
        context_fn: Callable returning runtime context dict (app_state, uptime, etc).
        port: TCP port to bind (default: config.HTTP_PORT).

    Returns:
        (thread, server, token) tuple.
    """
    port = port or HTTP_PORT
    token = _load_or_create_token()
    os.makedirs(UPLOADS_DIR, exist_ok=True)

    server = CortexHTTPServer(
        ("0.0.0.0", port),
        CortexHTTPHandler,
        cortex_protocol,
        context_fn,
        token,
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="http-api")
    thread.start()
    print("HTTP API server started on port {}".format(port))
    return thread, server, token
