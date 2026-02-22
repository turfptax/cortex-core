"""Activity logger for the wearable audio recorder.

Writes JSONL (one JSON object per line) to ~/logs/ with 15-minute rotation
aligned to audio segments. Each log file is self-contained and independently
readable. Flushes + fsyncs after every write to survive power loss.
"""

import json
import os
import time
from datetime import datetime

from config import LOG_DIR, SEGMENT_SECONDS

BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"


class ActivityLogger:
    """Append-only JSONL activity logger with time-based rotation."""

    def __init__(self):
        self._log_dir = LOG_DIR
        self._rotation_seconds = SEGMENT_SECONDS
        self._file = None
        self._file_path = None
        self._file_opened_at = 0.0
        self._session_id = None
        self._boot_id = self._read_boot_id()
        os.makedirs(self._log_dir, exist_ok=True)

    def log(self, event, data=None):
        """Write one event to the current log file. Rotates if needed.

        Wrapped in try/except so logging never crashes the recorder.
        """
        try:
            self._ensure_file()
            entry = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "mono": round(time.monotonic(), 3),
                "event": event,
                "session": self._session_id,
                "data": data or {},
            }
            line = json.dumps(entry, separators=(",", ":")) + "\n"
            self._file.write(line)
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError:
            # Disk full or I/O error — silently fail.
            # Recording must continue even if logging fails.
            self._close_file()

    def set_session(self, session_id):
        """Set or clear the current recording session ID."""
        self._session_id = session_id

    def rotate_now(self):
        """Force rotation (call when a new audio segment starts)."""
        self._close_file()

    def close(self):
        """Flush and close. Call on shutdown."""
        self._close_file()

    # ---- Internal ----

    def _ensure_file(self):
        """Open a new file if none is open, or rotate if time has elapsed."""
        now_mono = time.monotonic()
        if self._file is not None:
            if now_mono - self._file_opened_at >= self._rotation_seconds:
                self._close_file()
        if self._file is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._file_path = os.path.join(self._log_dir, f"{stamp}.jsonl")
            self._file = open(self._file_path, "a", encoding="utf-8")
            self._file_opened_at = now_mono
            # Write a self-documenting header
            header = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "mono": round(now_mono, 3),
                "event": "log_started",
                "session": self._session_id,
                "data": {
                    "boot_id": self._boot_id,
                    "log_file": os.path.basename(self._file_path),
                    "rotation_seconds": self._rotation_seconds,
                },
            }
            self._file.write(json.dumps(header, separators=(",", ":")) + "\n")
            self._file.flush()

    def _close_file(self):
        if self._file is not None:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
            except OSError:
                pass
            self._file = None
            self._file_path = None

    @staticmethod
    def _read_boot_id():
        try:
            with open(BOOT_ID_FILE, "r") as f:
                return f.read().strip()
        except OSError:
            return "unknown"
