"""Lightweight debug event logger for Slice 12.1 troubleshooting.

Activate via env var CORTEX_DEBUG=1 (set in systemd unit or via
`sudo systemctl set-environment CORTEX_DEBUG=1` and restart).

When OFF (default): every dbg.event() call is an O(1) attribute check.
When ON: events are appended to a thread-safe ring buffer (last 200)
         AND emitted at INFO level so journalctl picks them up.

Use:
    from debug import dbg
    dbg.event("BTN press")
    dbg.event("STATE armed -> flag-recording", t_ms=18)
    dbg.event("AUDIO start", path="/tmp/...")
    dbg.event("STT groq", chars=42, dur_s=1.84)
    dbg.event("NET POST /journal", code=200, dur_ms=312)

Inspect:
    sudo journalctl -u cortex-core -f | grep DBG    # live tail
    sudo journalctl -u cortex-core --since "5min ago" | grep DBG

Toggle without restart:
    sudo systemctl set-environment CORTEX_DEBUG=1
    sudo systemctl restart cortex-core              # required to pick up

Slice 12.1 design choice: instead of an LCD overlay (deferred to 12.2),
we route everything through systemd journal which Tory can grep from
the .132 SSH session during a button-test debug pass.
"""

import logging
import os
import threading
import time
from collections import deque
from typing import Any

_log = logging.getLogger("cortex.dbg")


class DebugLogger:
    """Single global instance — see `dbg` below."""

    def __init__(self):
        self.enabled = os.environ.get("CORTEX_DEBUG", "0") == "1"
        self._buf: deque = deque(maxlen=200)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()

    def event(self, name: str, **kwargs: Any) -> None:
        """Log a debug event. No-op when disabled."""
        if not self.enabled:
            return
        rec = {
            "t": round(time.monotonic() - self._t0, 4),
            "name": name,
            **kwargs,
        }
        with self._lock:
            self._buf.append(rec)
        if kwargs:
            _log.info("DBG %s %s", name, kwargs)
        else:
            _log.info("DBG %s", name)

    def snapshot(self) -> list:
        """Return a copy of the current ring buffer (for HTTP debug endpoint)."""
        with self._lock:
            return list(self._buf)

    def reload(self) -> bool:
        """Re-read CORTEX_DEBUG from env. Returns the new state."""
        self.enabled = os.environ.get("CORTEX_DEBUG", "0") == "1"
        return self.enabled


# Global singleton — every module imports this.
dbg = DebugLogger()
