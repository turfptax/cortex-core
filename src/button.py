"""Single-button interaction with short press, long press, and shutdown hold."""

import time

from config import SHORT_PRESS_MAX_MS, LONG_PRESS_MS, SHUTDOWN_PRESS_MS


class ButtonHandler:
    def __init__(self, board):
        self.board = board
        self.press_time = None
        self._long_fired = False
        self._shutdown_fired = False
        self._callbacks = {}

        board.on_button_press(self._on_press)
        board.on_button_release(self._on_release)

    def on(self, event, callback):
        """Register a callback for 'short_press', 'long_press', or 'shutdown'."""
        self._callbacks[event] = callback

    def _on_press(self, channel=None):
        self.press_time = time.monotonic()
        self._long_fired = False
        self._shutdown_fired = False
        # Notify main loop that user interacted (for display wake)
        if "any_press" in self._callbacks:
            self._callbacks["any_press"]()

    def _on_release(self, channel=None):
        if self.press_time is None:
            return
        duration_ms = (time.monotonic() - self.press_time) * 1000
        self.press_time = None

        if not self._long_fired and duration_ms < SHORT_PRESS_MAX_MS:
            cb = self._callbacks.get("short_press")
            if cb:
                cb()

    def check_held(self):
        """Call from main loop (~10Hz) to detect long press while button is held."""
        if self.press_time is None:
            return
        held_ms = (time.monotonic() - self.press_time) * 1000

        if held_ms >= SHUTDOWN_PRESS_MS and not self._shutdown_fired:
            self._shutdown_fired = True
            cb = self._callbacks.get("shutdown")
            if cb:
                cb()
        elif held_ms >= LONG_PRESS_MS and not self._long_fired:
            self._long_fired = True
            cb = self._callbacks.get("long_press")
            if cb:
                cb()
