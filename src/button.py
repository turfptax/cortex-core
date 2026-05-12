"""Single-button interaction with tap (flag) + hold (record) semantics.

Slice 12.1 button event model:

  any_press         — fires immediately on every press (display wake hook)
  short_press       — fires on release if duration < SHORT_PRESS_MAX_MS
                      (and the hold threshold did NOT fire). This is "tap".
  hold_threshold    — fires while held when duration >= SHORT_PRESS_MAX_MS.
                      Audio keeps recording. Use this for the "you've now
                      committed to a hold" visual + LED cue.
  hold_release      — fires on release if hold_threshold already fired.
                      Receives the actual hold duration in seconds. This
                      is the end-of-hold action; use it to stop arecord
                      and route the captured audio.
  shutdown          — fires while held when duration >= SHUTDOWN_PRESS_MS.

The legacy `long_press` event is still emitted (at LONG_PRESS_MS while
held) for any caller that depends on it, but companion mode in states.py
has migrated to hold_threshold + hold_release. Removing long_press would
break gamepad-action handlers that call sm.handle_long_press in legacy
RECORDING/PAUSED states.
"""

import time

from config import SHORT_PRESS_MAX_MS, LONG_PRESS_MS, SHUTDOWN_PRESS_MS
from debug import dbg


class ButtonHandler:
    def __init__(self, board):
        self.board = board
        self.press_time = None
        self._threshold_fired = False
        self._long_fired = False
        self._shutdown_fired = False
        self._callbacks = {}

        board.on_button_press(self._on_press)
        board.on_button_release(self._on_release)

    def on(self, event, callback):
        """Register a callback for an event. See module docstring."""
        self._callbacks[event] = callback

    def _on_press(self, channel=None):
        self.press_time = time.monotonic()
        self._threshold_fired = False
        self._long_fired = False
        self._shutdown_fired = False
        dbg.event("BTN press")
        cb = self._callbacks.get("any_press")
        if cb:
            cb()

    def _on_release(self, channel=None):
        if self.press_time is None:
            return
        duration_ms = (time.monotonic() - self.press_time) * 1000
        self.press_time = None

        # Slice 12.1 fix: dispatch on duration alone, not on _threshold_fired.
        # The _threshold_fired flag only controls the in-press visual cue
        # (hold_threshold callback). Using it to gate _on_release created
        # a dead zone: if the user released between SHORT_PRESS_MAX_MS and
        # the next check_held tick (up to 125ms later at 8Hz), neither
        # short_press nor hold_release fired and the state machine froze.
        if duration_ms < SHORT_PRESS_MAX_MS:
            dbg.event("BTN release (tap)", dur_ms=round(duration_ms, 1))
            cb = self._callbacks.get("short_press")
            if cb:
                cb()
        else:
            dbg.event("BTN release (hold)", dur_ms=round(duration_ms, 1),
                      threshold_fired=self._threshold_fired)
            cb = self._callbacks.get("hold_release")
            if cb:
                cb(duration_ms / 1000.0)

    def check_held(self):
        """Call from main loop (~10Hz) to detect hold thresholds.

        Order matters: shutdown is checked first so that holding past
        SHUTDOWN_PRESS_MS doesn't get a no-op tick that fires hold_threshold
        and long_press only.
        """
        if self.press_time is None:
            return
        held_ms = (time.monotonic() - self.press_time) * 1000

        if held_ms >= SHUTDOWN_PRESS_MS and not self._shutdown_fired:
            self._shutdown_fired = True
            dbg.event("BTN shutdown_threshold", held_ms=round(held_ms, 1))
            cb = self._callbacks.get("shutdown")
            if cb:
                cb()
            return

        if held_ms >= LONG_PRESS_MS and not self._long_fired:
            self._long_fired = True
            dbg.event("BTN long_press_threshold", held_ms=round(held_ms, 1))
            cb = self._callbacks.get("long_press")
            if cb:
                cb()

        if held_ms >= SHORT_PRESS_MAX_MS and not self._threshold_fired:
            self._threshold_fired = True
            dbg.event("BTN hold_threshold", held_ms=round(held_ms, 1))
            cb = self._callbacks.get("hold_threshold")
            if cb:
                cb()
