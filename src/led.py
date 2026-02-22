"""RGB LED status indicator with blink patterns."""

import time


class LEDManager:
    def __init__(self, board):
        self.board = board
        self.state = None
        self._blink_on = True
        self._last_toggle = 0.0
        self._flash_until = 0.0  # monotonic time when flash expires
        self._flash_color = None

    def set_state(self, state):
        """Set LED state: 'recording', 'paused', 'idle', 'error', 'shutdown'."""
        self.state = state
        self._blink_on = True
        self._last_toggle = time.monotonic()
        self._apply()

    def ble_flash(self, event="connect"):
        """Brief color flash for BLE events without overriding current state.

        event: 'connect' (green), 'disconnect' (yellow), 'message' (blue)
        """
        colors = {
            "connect": (0, 200, 0),
            "disconnect": (200, 150, 0),
            "message": (0, 100, 255),
        }
        self._flash_color = colors.get(event, (0, 100, 255))
        self._flash_until = time.monotonic() + 0.3
        self.board.set_rgb(*self._flash_color)

    def tick(self):
        """Call from main loop for blink animations."""
        now = time.monotonic()
        # Handle flash expiry
        if self._flash_color and now >= self._flash_until:
            self._flash_color = None
            self._apply()
            return
        if self.state == "paused":
            if now - self._last_toggle >= 1.0:
                self._blink_on = not self._blink_on
                self._last_toggle = now
                self._apply()
        elif self.state == "error":
            if now - self._last_toggle >= 0.2:
                self._blink_on = not self._blink_on
                self._last_toggle = now
                self._apply()
        elif self.state == "stt_listening":
            if now - self._last_toggle >= 0.5:
                self._blink_on = not self._blink_on
                self._last_toggle = now
                self._apply()

    def _apply(self):
        if self.state == "recording":
            self.board.set_rgb(255, 0, 0)
        elif self.state == "paused":
            if self._blink_on:
                self.board.set_rgb(255, 180, 0)
            else:
                self.board.set_rgb(0, 0, 0)
        elif self.state == "idle":
            self.board.set_rgb(0, 30, 0)
        elif self.state == "stt_idle":
            self.board.set_rgb(0, 60, 60)
        elif self.state == "stt_listening":
            if self._blink_on:
                self.board.set_rgb(0, 200, 200)
            else:
                self.board.set_rgb(0, 30, 30)
        elif self.state == "note_taking":
            self.board.set_rgb(0, 200, 200)
        elif self.state == "error":
            if self._blink_on:
                self.board.set_rgb(255, 0, 0)
            else:
                self.board.set_rgb(0, 0, 0)
        elif self.state == "shutdown":
            self.board.set_rgb(0, 0, 255)
        else:
            self.board.set_rgb(0, 0, 0)
