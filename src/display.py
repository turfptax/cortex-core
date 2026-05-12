"""Display UI renderer for the 240x280 ST7789P3 via WhisPlay driver.

Slice 12.1: _flush() rewritten to use numpy-vectorized RGB->RGB565
conversion. The pure-Python loop was the dominant cost (~365ms per
frame on Pi Zero 2W), which blocked the main loop, blocked button
event dispatch, and made the device feel frozen during companion-mode
press handling. Numpy version benches at ~15-30ms — fast enough that
DISPLAY_UPDATE_HZ=8 is comfortable and force-render adds real value.
"""

import time

from PIL import Image, ImageDraw, ImageFont

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

from config import (
    DISPLAY_WIDTH, DISPLAY_HEIGHT, SEGMENT_SECONDS,
    FONT_PATH, FONT_PATH_REGULAR, FONT_LARGE, FONT_MEDIUM, FONT_SMALL,
    COLOR_BG, COLOR_TEXT, COLOR_DIM, COLOR_RED, COLOR_GREEN,
    COLOR_YELLOW, COLOR_BLUE, COLOR_BAR_BG, COLOR_CYAN, COLOR_CYAN_DIM,
    COLOR_MAGENTA,
)
from debug import dbg


def _format_duration(seconds):
    """Format seconds as HH:MM:SS."""
    s = int(seconds)
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _format_size(bytes_val):
    """Format bytes as human-readable size."""
    if bytes_val >= 1_073_741_824:
        return f"{bytes_val / 1_073_741_824:.1f} GB"
    if bytes_val >= 1_048_576:
        return f"{bytes_val / 1_048_576:.0f} MB"
    return f"{bytes_val / 1024:.0f} KB"


def _word_wrap(text, font, max_width):
    """Wrap text to fit within max_width pixels. Returns list of lines."""
    if not text:
        return []
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = font.getbbox(test)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


class Display:
    W = DISPLAY_WIDTH
    H = DISPLAY_HEIGHT

    def __init__(self, board):
        self.board = board
        self.img = Image.new("RGB", (self.W, self.H), COLOR_BG)
        self.draw = ImageDraw.Draw(self.img)
        try:
            self.font_lg = ImageFont.truetype(FONT_PATH, FONT_LARGE)
            self.font_md = ImageFont.truetype(FONT_PATH_REGULAR, FONT_MEDIUM)
            self.font_sm = ImageFont.truetype(FONT_PATH_REGULAR, FONT_SMALL)
        except OSError:
            self.font_lg = ImageFont.load_default()
            self.font_md = ImageFont.load_default()
            self.font_sm = ImageFont.load_default()

        # Pre-allocate output buffer
        self._buf = bytearray(self.W * self.H * 2)

    def render(self, state):
        """Render full frame from application state dict.

        Slice 12.1 dispatch: companion_status WINS over app_state.
        Whenever a companion flow is active (status non-empty), we route
        to the appropriate companion screen regardless of what app_state
        says. This fixes the v12 bug where the recording screen never
        painted on a fast tap because app_state was still HOME.
        """
        self.draw.rectangle([0, 0, self.W, self.H], fill=COLOR_BG)

        app = state.get("app_state", "STT_IDLE")
        cstatus = state.get("companion_status", "") or ""

        # Companion-status routing — absolute, regardless of app_state.
        # Slice 12.1.2 trace: dbg-log which renderer ran so we can see
        # if a frame "looks black" because the renderer drew nothing,
        # or because dispatch went somewhere unexpected.
        renderer = "unknown"
        if cstatus == "recording":
            # Slice 12.1.2 toggle-record: active recording with timer.
            self._render_companion_recording_active(state)
            renderer = "rec_active"
        elif cstatus == "pressed":
            self._render_companion_pressed(state)
            renderer = "pressed"
        elif cstatus in ("recording-armed", "armed",
                         "flag-recording", "flag-transcribing"):
            # 'armed', 'flag-recording', 'flag-transcribing' are legacy
            # v12 status names kept for backwards compat during deploy.
            self._render_companion_recording(state)
            renderer = "rec_legacy"
        elif cstatus in ("transcribing", "asking-overseer",
                         "saving-journal"):
            self._render_companion_processing(state)
            renderer = "processing"
        elif cstatus in ("reply-speaking", "saved", "flag-saved"):
            self._render_companion_done(state)
            renderer = "done"
        elif cstatus:
            # Unknown companion_status — render a generic processing
            # screen so the LCD never goes blank mid-flow.
            self._render_companion_processing(state)
            renderer = f"unknown:{cstatus}"
        elif app in ("HOME", "STT_IDLE"):
            self._render_companion_home(state)
            renderer = "home"
        elif app == "STT_LISTENING":
            self._render_stt_listening(state)
            renderer = "stt_list"
        elif app == "NOTE_TAKING":
            self._render_note_taking(state)
            renderer = "note"
        elif app in ("RECORDING", "PAUSED", "IDLE"):
            self._draw_status_bar(state)
            self._draw_segment_info(state)
            self._draw_progress_bar(state)
            self._draw_session_stats(state)
            self._draw_footer(state)
            renderer = "rec_legacy_app"
        else:
            self._render_companion_home(state)
            renderer = "home_fallback"
        # Throttle: only log renderer name on dispatch CHANGE so we don't
        # spam the journal with "rec_active rec_active rec_active...".
        if renderer != getattr(self, "_last_renderer", None):
            dbg.event("RENDER dispatch", to=renderer,
                      cstatus=cstatus, app=app)
            self._last_renderer = renderer

        self._flush()

    # ---- Slice 12 / 12.1: companion-mode screens ----

    def _render_companion_recording_active(self, state):
        """Slice 12.1.2: active toggle-record screen with elapsed timer.

        Layout (240×280 portrait):
          - Clock top-left, dim
          - Big green REC dot top-center
          - HUGE elapsed timer (MM:SS) center
          - Hint at bottom: 'press button to save'
        """
        time_str = state.get("time_str", "--:--")
        elapsed = max(0.0, float(state.get("recording_elapsed_s", 0)))
        mm = int(elapsed) // 60
        ss = int(elapsed) % 60
        timer = f"{mm:02d}:{ss:02d}"

        # Clock
        self.draw.text((10, 6), time_str,
                       fill=COLOR_DIM, font=self.font_sm)

        # REC dot top-center
        cx, cy, r = self.W // 2, 50, 18
        self.draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=COLOR_RED)
        self.draw.text((cx - 16, cy - 9), "REC",
                       fill=COLOR_BG, font=self.font_sm)

        # Big timer (use the largest available font; if too small, just
        # render font_lg twice as bold-looking by drawing it 2px offset)
        bw_lg = self.font_lg.getbbox(timer)[2]
        # Draw timer in bright green at vertical center
        ty = 110
        # Center horizontally
        tx = (self.W - bw_lg) // 2
        # Render twice for "bold" effect (no actual bold font available)
        self.draw.text((tx, ty), timer,
                       fill=COLOR_GREEN, font=self.font_lg)
        self.draw.text((tx + 1, ty), timer,
                       fill=COLOR_GREEN, font=self.font_lg)

        # Secondary timer label
        sub = "recording…"
        sw = self.font_md.getbbox(sub)[2]
        self.draw.text(((self.W - sw) // 2, 160), sub,
                       fill=COLOR_TEXT, font=self.font_md)

        # Bottom hint
        self.draw.line([(14, 220), (self.W - 14, 220)],
                       fill=COLOR_DIM, width=1)
        hint = "press button to save"
        hw = self.font_sm.getbbox(hint)[2]
        self.draw.text(((self.W - hw) // 2, 232), hint,
                       fill=COLOR_GREEN, font=self.font_sm)
        hint2 = 'or say "hey overseer" to chat'
        hw2 = self.font_sm.getbbox(hint2)[2]
        self.draw.text(((self.W - hw2) // 2, 250), hint2,
                       fill=COLOR_DIM, font=self.font_sm)

    def _render_companion_pressed(self, state):
        """Slice 12.1: shown WHILE the button is held but before the
        SHORT_PRESS_MAX_MS threshold. Tells the user 'you're pressing —
        release now to flag, keep holding to record'.

        The bottom hint flips at the threshold (handled by render dispatch
        which switches to _render_companion_recording when companion_status
        becomes 'recording-armed')."""
        time_str = state.get("time_str", "--:--")
        self.draw.text((10, 6), time_str,
                       fill=COLOR_DIM, font=self.font_sm)

        # Big yellow circle centered, growing implication
        cx, cy, r = self.W // 2, 90, 30
        self.draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                          outline=COLOR_YELLOW, width=4)
        self.draw.text((cx - 18, cy - 9), "...",
                       fill=COLOR_YELLOW, font=self.font_md)

        # Primary cue
        msg = "release: flag this"
        bw = self.font_md.getbbox(msg)[2]
        self.draw.text(((self.W - bw) // 2, 150), msg,
                       fill=COLOR_TEXT, font=self.font_md)

        # Secondary cue
        msg2 = "keep holding: record"
        bw = self.font_sm.getbbox(msg2)[2]
        self.draw.text(((self.W - bw) // 2, 180), msg2,
                       fill=COLOR_DIM, font=self.font_sm)

    def _render_companion_home(self, state):
        """Idle home screen for the overseer companion role.
        Layout (240×280 portrait):
          - Top bar: clock + connectivity dot + queue depth
          - Hero: 'OVERSEER' badge + status digest (notes/review)
          - Middle: button-vocabulary hint (hold/tap/wake-phrase)
          - Bottom: latest notification preview OR last-tick stamp
        """
        time_str = state.get("time_str", "--:--")
        notes_total = state.get("overseer_notes_total", 0)
        unread = state.get("overseer_unread", 0)
        pending = state.get("overseer_pending", 0)
        loop_running = state.get("overseer_loop_running", False)
        last_tick = state.get("overseer_last_tick", "")
        queue_depth = state.get("journal_queue_depth", 0)
        notif = state.get("notification_preview", "")

        # Top bar
        self.draw.text((10, 6), time_str,
                       fill=COLOR_TEXT, font=self.font_md)
        dot_color = COLOR_GREEN if loop_running else COLOR_DIM
        cx = self.W - 18
        self.draw.ellipse([cx - 5, 12, cx + 5, 22], fill=dot_color)
        if queue_depth > 0:
            self.draw.text((cx - 50, 8), f"q{queue_depth}",
                           fill=COLOR_YELLOW, font=self.font_sm)

        # Hero badge
        y = 38
        badge = "OVERSEER"
        bbox = self.font_lg.getbbox(badge)
        bw = bbox[2] - bbox[0]
        bx = (self.W - bw) // 2 - 6
        self.draw.rectangle(
            [bx, y, bx + bw + 12, y + 30],
            outline=COLOR_CYAN, width=2,
        )
        self.draw.text((bx + 6, y + 4), badge,
                       fill=COLOR_CYAN, font=self.font_lg)

        # Status digest line
        y = 80
        digest = f"{notes_total} notes · {pending} review"
        if unread:
            digest = f"{unread} new · " + digest
        bw = self.font_sm.getbbox(digest)[2]
        self.draw.text(((self.W - bw) // 2, y), digest,
                       fill=COLOR_DIM, font=self.font_sm)

        # Button vocabulary hint (Slice 12.1.2: toggle-record model)
        y = 116
        self.draw.text((14, y), "tap: start recording",
                       fill=COLOR_GREEN, font=self.font_md)
        y += 26
        self.draw.text((14, y), "tap again: save",
                       fill=COLOR_TEXT, font=self.font_md)
        y += 26
        self.draw.text((14, y), 'say "hey overseer" to chat',
                       fill=COLOR_CYAN, font=self.font_sm)

        # Bottom strip — notification preview OR last tick
        y = 210
        self.draw.line([(14, y), (self.W - 14, y)],
                       fill=COLOR_DIM, width=1)
        y += 6
        if notif:
            wrapped = _word_wrap(notif, self.font_sm, self.W - 28)
            for line in wrapped[:3]:
                self.draw.text((14, y), line,
                               fill=COLOR_YELLOW, font=self.font_sm)
                y += 16
        elif last_tick:
            ts = last_tick[11:16] if len(last_tick) >= 16 else last_tick
            self.draw.text((14, y), f"Last tick: {ts}Z",
                           fill=COLOR_DIM, font=self.font_sm)

    def _render_companion_recording(self, state):
        """Big REC screen — visible from across a room.

        Slice 12.1: copy revised. 'recording-armed' is the new primary
        state name; the legacy v12 names still map for safety during
        the rolling deploy window."""
        time_str = state.get("time_str", "--:--")
        cstatus = state.get("companion_status", "") or ""
        self.draw.text((10, 6), time_str,
                       fill=COLOR_DIM, font=self.font_sm)

        cx, cy, r = self.W // 2, 90, 36
        self.draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                          fill=COLOR_GREEN)  # was COLOR_RED — green = recording journal
        self.draw.text((cx - 22, cy - 14), "REC",
                       fill=COLOR_BG, font=self.font_md)

        label_map = {
            "recording-armed": "recording…",
            "armed": "recording…",                    # legacy
            "flag-recording": "flagging…",            # legacy
            "flag-transcribing": "transcribing flag…", # legacy
        }
        label = label_map.get(cstatus, cstatus)
        bw = self.font_md.getbbox(label)[2]
        self.draw.text(((self.W - bw) // 2, 160), label,
                       fill=COLOR_TEXT, font=self.font_md)

        hint = "release to save"
        bw = self.font_sm.getbbox(hint)[2]
        self.draw.text(((self.W - bw) // 2, 240), hint,
                       fill=COLOR_DIM, font=self.font_sm)

    def _render_companion_processing(self, state):
        """'Thinking' screen — bold label + dots."""
        time_str = state.get("time_str", "--:--")
        cstatus = state.get("companion_status", "")
        self.draw.text((10, 6), time_str,
                       fill=COLOR_DIM, font=self.font_sm)

        label_map = {
            "transcribing": "TRANSCRIBING",
            "asking-overseer": "ASKING OVERSEER",
        }
        label = label_map.get(cstatus, cstatus.upper())
        bw = self.font_lg.getbbox(label)[2]
        self.draw.text(((self.W - bw) // 2, 100), label,
                       fill=COLOR_BLUE, font=self.font_lg)

        dots = "..." if (int(time.time()) % 2 == 0) else "."
        bw = self.font_lg.getbbox(dots)[2]
        self.draw.text(((self.W - bw) // 2, 150), dots,
                       fill=COLOR_BLUE, font=self.font_lg)

    def _render_companion_done(self, state):
        """Completion screen — reply text or save confirmation."""
        time_str = state.get("time_str", "--:--")
        cstatus = state.get("companion_status", "")
        message = state.get("companion_message", "")
        routed = state.get("companion_routed", "")
        self.draw.text((10, 6), time_str,
                       fill=COLOR_DIM, font=self.font_sm)

        title_map = {
            "saving-journal": "JOURNAL SAVED",
            "saved": "SAVED",
            "flag-saved": "FLAG SAVED",
            "reply-speaking": "OVERSEER",
        }
        title = title_map.get(cstatus, cstatus.upper())
        title_color = (COLOR_CYAN if routed == "overseer-chat"
                       else COLOR_GREEN)
        self.draw.text((14, 28), title,
                       fill=title_color, font=self.font_lg)

        if message:
            wrapped = _word_wrap(message, self.font_sm, self.W - 28)
            y = 70
            for line in wrapped[:10]:
                self.draw.text((14, y), line,
                               fill=COLOR_TEXT, font=self.font_sm)
                y += 18
                if y > 240:
                    break
        else:
            self.draw.text((14, 80), "entry persisted",
                           fill=COLOR_TEXT, font=self.font_md)

    # ---- STT_IDLE Screen ----

    def _render_stt_idle(self, state):
        """Home screen: READY badge, instructions, stats."""
        y = 6
        time_str = state.get("time_str", "--:--")

        # Badge
        badge_text = " READY "
        left_margin = 14
        bbox = self.font_lg.getbbox(badge_text)
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        self.draw.rectangle(
            [left_margin, y, left_margin + bw + 4, y + bh + 4],
            fill=COLOR_CYAN,
        )
        self.draw.text(
            (left_margin + 2, y + 1), badge_text,
            fill=COLOR_BG, font=self.font_lg,
        )

        # Clock
        tw = self.font_md.getbbox(time_str)[2]
        self.draw.text(
            (self.W - tw - 16, y + 2), time_str,
            fill=COLOR_DIM, font=self.font_md,
        )

        # Instructions
        y = 50
        self.draw.text((14, y), "Press button to speak", fill=COLOR_TEXT, font=self.font_md)
        y += 30
        self.draw.line([(14, y), (self.W - 14, y)], fill=COLOR_DIM, width=1)
        y += 10
        self.draw.text((14, y), 'Say "note" to take a note', fill=COLOR_CYAN, font=self.font_sm)
        y += 20
        self.draw.text((14, y), 'Say "record" to record', fill=COLOR_CYAN, font=self.font_sm)

        # Stats
        y = 170
        self.draw.line([(14, y), (self.W - 14, y)], fill=COLOR_DIM, width=1)
        y += 6
        note_count = state.get("note_count", 0)
        rec_count = state.get("rec_count", 0)
        disk_free = state.get("disk_free", 0)
        remaining_h = state.get("remaining_hours", 0)

        # BLE connection info
        ble_connected = state.get("ble_connected", False)
        ble_info = state.get("ble_info")
        if ble_connected and ble_info:
            name = (ble_info.get("name") or "ESP32")[:20]
            addr = ble_info.get("address") or "--"
            mtu = ble_info.get("mtu")
            rssi = ble_info.get("rssi")
            # Line 1: device name in cyan
            self.draw.text((14, y), name, fill=COLOR_CYAN, font=self.font_sm)
            y += 16
            # Line 2: address + MTU
            detail = addr
            if mtu:
                detail += f"  MTU:{mtu - 3}"
            self.draw.text((14, y), detail, fill=COLOR_DIM, font=self.font_sm)
            y += 16
        else:
            self.draw.text(
                (self.W - 54, y), "BLE: --",
                fill=(50, 50, 50), font=self.font_sm,
            )

        self.draw.text(
            (14, y),
            f"Notes: {note_count}    Recs: {rec_count}",
            fill=COLOR_DIM, font=self.font_sm,
        )
        y += 16
        self.draw.text(
            (14, y),
            f"Free: {_format_size(disk_free)}  (~{int(remaining_h)}h)",
            fill=COLOR_DIM, font=self.font_sm,
        )

        # Footer
        self._draw_stt_footer("STT_IDLE")

    # ---- STT_LISTENING Screen ----

    def _render_stt_listening(self, state):
        """Listening screen: badge, partial transcript, command hints."""
        y = 6
        time_str = state.get("time_str", "--:--")

        # Badge
        badge_text = " LISTEN "
        left_margin = 14
        bbox = self.font_lg.getbbox(badge_text)
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        self.draw.rectangle(
            [left_margin, y, left_margin + bw + 4, y + bh + 4],
            fill=COLOR_CYAN,
        )
        self.draw.text(
            (left_margin + 2, y + 1), badge_text,
            fill=COLOR_BG, font=self.font_lg,
        )

        # Clock
        tw = self.font_md.getbbox(time_str)[2]
        self.draw.text(
            (self.W - tw - 16, y + 2), time_str,
            fill=COLOR_DIM, font=self.font_md,
        )

        # Partial transcript area
        y = 45
        self.draw.line([(14, y), (self.W - 14, y)], fill=COLOR_DIM, width=1)
        y += 10

        partial = state.get("stt_partial", "")
        if partial:
            lines = _word_wrap(f'"{partial}"', self.font_md, self.W - 28)
            for line in lines[:6]:
                self.draw.text((14, y), line, fill=COLOR_TEXT, font=self.font_md)
                y += 20
        else:
            self.draw.text((14, y + 20), "Listening...", fill=COLOR_CYAN, font=self.font_md)

        # Command hints
        y = 170
        self.draw.line([(14, y), (self.W - 14, y)], fill=COLOR_DIM, width=1)
        y += 6
        self.draw.text((14, y), "Say a command:", fill=COLOR_DIM, font=self.font_sm)
        y += 16
        self.draw.text((14, y), '"note" or "record"', fill=COLOR_CYAN_DIM, font=self.font_sm)

        # Footer
        self._draw_stt_footer("STT_LISTENING")

    # ---- NOTE_TAKING Screen ----

    def _render_note_taking(self, state):
        """Note-taking screen: badge, live scrolling transcript."""
        y = 6
        time_str = state.get("time_str", "--:--")

        # Badge
        badge_text = " NOTE "
        left_margin = 14
        bbox = self.font_lg.getbbox(badge_text)
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        self.draw.rectangle(
            [left_margin, y, left_margin + bw + 4, y + bh + 4],
            fill=COLOR_CYAN,
        )
        self.draw.text(
            (left_margin + 2, y + 1), badge_text,
            fill=COLOR_BG, font=self.font_lg,
        )

        # Clock
        tw = self.font_md.getbbox(time_str)[2]
        self.draw.text(
            (self.W - tw - 16, y + 2), time_str,
            fill=COLOR_DIM, font=self.font_md,
        )

        # Note transcript area
        y = 38
        self.draw.line([(14, y), (self.W - 14, y)], fill=COLOR_DIM, width=1)
        y += 6

        note_text = state.get("note_text", "")
        partial = state.get("stt_partial", "")
        # Combine saved finals + current partial
        display_text = note_text
        if partial:
            display_text = f"{display_text} {partial}".strip() if display_text else partial

        if display_text:
            lines = _word_wrap(display_text, self.font_sm, self.W - 28)
            # Show last N lines that fit (scroll from bottom)
            max_lines = 11
            visible = lines[-max_lines:]
            for line in visible:
                self.draw.text((14, y), line, fill=COLOR_TEXT, font=self.font_sm)
                y += 16
        else:
            self.draw.text((14, y + 10), "Speak your note...", fill=COLOR_CYAN, font=self.font_md)

        # Footer
        self._draw_stt_footer("NOTE_TAKING")

    # ---- Shared STT footer ----

    def _draw_stt_footer(self, mode):
        """Footer area for STT screens."""
        y = self.H - 52
        self.draw.line([(14, y), (self.W - 14, y)], fill=COLOR_DIM, width=1)
        y += 6

        if mode == "STT_IDLE":
            self.draw.text((14, y), "Press: Speak", fill=COLOR_CYAN, font=self.font_sm)
            self.draw.text((14, y + 16), "Hold 5s: Shutdown", fill=COLOR_DIM, font=self.font_sm)
        elif mode == "STT_LISTENING":
            self.draw.text((14, y), "Press: Cancel", fill=COLOR_YELLOW, font=self.font_sm)
            self.draw.text((14, y + 16), "Hold 5s: Shutdown", fill=COLOR_DIM, font=self.font_sm)
        elif mode == "NOTE_TAKING":
            self.draw.text((14, y), "Press: Save note", fill=COLOR_GREEN, font=self.font_sm)
            self.draw.text((14, y + 16), "Silence: auto-save", fill=COLOR_DIM, font=self.font_sm)

    # ---- Existing Recording UI (unchanged) ----

    def _draw_status_bar(self, state):
        """Top bar: state indicator + elapsed + clock."""
        y = 6
        app = state.get("app_state", "IDLE")

        if app == "RECORDING":
            badge_color = COLOR_RED
            badge_text = " REC "
        elif app == "PAUSED":
            badge_color = COLOR_YELLOW
            badge_text = " PAUSE "
        else:
            badge_color = COLOR_GREEN
            badge_text = " IDLE "

        left_margin = 14
        bbox = self.font_lg.getbbox(badge_text)
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        self.draw.rectangle(
            [left_margin, y, left_margin + bw + 4, y + bh + 4],
            fill=badge_color,
        )
        self.draw.text(
            (left_margin + 2, y + 1), badge_text,
            fill=COLOR_BG, font=self.font_lg,
        )

        elapsed = _format_duration(state.get("session_elapsed", 0))
        self.draw.text((108, y + 2), elapsed, fill=COLOR_TEXT, font=self.font_md)

        time_str = state.get("time_str", "--:--")
        tw = self.font_md.getbbox(time_str)[2]
        self.draw.text(
            (self.W - tw - 16, y + 2), time_str,
            fill=COLOR_DIM, font=self.font_md,
        )

    def _draw_segment_info(self, state):
        """Segment details section."""
        y = 40
        app = state.get("app_state", "IDLE")
        seg_count = state.get("segment_count", 0)
        seg_elapsed = state.get("segment_elapsed", 0)

        self.draw.text((8, y), f"Segment #{seg_count}", fill=COLOR_TEXT, font=self.font_md)

        if app == "RECORDING":
            seg_dur = _format_duration(seg_elapsed)
            seg_total = _format_duration(SEGMENT_SECONDS)
            self.draw.text(
                (8, y + 20),
                f"Duration: {seg_dur} / {seg_total}",
                fill=COLOR_DIM, font=self.font_sm,
            )
        elif app == "PAUSED":
            self.draw.text((8, y + 20), "-- paused --", fill=COLOR_YELLOW, font=self.font_sm)
        else:
            self.draw.text((8, y + 20), "Ready to record", fill=COLOR_DIM, font=self.font_sm)

    def _draw_progress_bar(self, state):
        """Segment progress bar."""
        y = 88
        bar_x = 8
        bar_w = self.W - 16
        bar_h = 14

        self.draw.rectangle([bar_x, y, bar_x + bar_w, y + bar_h], fill=COLOR_BAR_BG)

        seg_elapsed = state.get("segment_elapsed", 0)
        if SEGMENT_SECONDS > 0:
            progress = min(seg_elapsed / SEGMENT_SECONDS, 1.0)
        else:
            progress = 0

        app = state.get("app_state", "IDLE")
        if app == "RECORDING":
            fill_color = COLOR_RED
        elif app == "PAUSED":
            fill_color = COLOR_YELLOW
        else:
            fill_color = COLOR_GREEN

        fill_w = int(bar_w * progress)
        if fill_w > 0:
            self.draw.rectangle(
                [bar_x, y, bar_x + fill_w, y + bar_h],
                fill=fill_color,
            )

        pct = f"{int(progress * 100)}%"
        self.draw.text(
            (bar_x + bar_w // 2 - 10, y + 1), pct,
            fill=COLOR_TEXT, font=self.font_sm,
        )

    def _draw_session_stats(self, state):
        """Storage and session statistics."""
        y = 116
        line_h = 18
        seg_count = state.get("segment_count", 0)
        elapsed = _format_duration(state.get("session_elapsed", 0))
        disk_used = state.get("disk_used", 0)
        disk_free = state.get("disk_free", 0)
        remaining_h = state.get("remaining_hours", 0)

        lines = [
            f"Total: {elapsed}   Segs: {seg_count}",
            f"Used: {_format_size(disk_used)}",
            f"Free: {_format_size(disk_free)}",
            f"Capacity: ~{int(remaining_h)}h remaining",
        ]

        self.draw.line([(8, y), (self.W - 8, y)], fill=COLOR_DIM, width=1)
        y += 4

        for line in lines:
            self.draw.text((8, y), line, fill=COLOR_DIM, font=self.font_sm)
            y += line_h

    def _draw_footer(self, state):
        """Bottom area: hints for recording mode."""
        y = self.H - 52
        app = state.get("app_state", "IDLE")

        self.draw.line([(8, y), (self.W - 8, y)], fill=COLOR_DIM, width=1)
        y += 6

        if app == "IDLE":
            self.draw.text((14, y), "Press: Start recording", fill=COLOR_GREEN, font=self.font_sm)
            self.draw.text((14, y + 16), "Hold 5s: Shutdown", fill=COLOR_DIM, font=self.font_sm)
        elif app == "RECORDING":
            self.draw.text((14, y), "Press: Pause", fill=COLOR_YELLOW, font=self.font_sm)
            self.draw.text((14, y + 16), "Hold: Stop | Hold 5s: Off", fill=COLOR_DIM, font=self.font_sm)
        elif app == "PAUSED":
            self.draw.text((14, y), "Press: Resume", fill=COLOR_GREEN, font=self.font_sm)
            self.draw.text((14, y + 16), "Hold: Stop | Hold 5s: Off", fill=COLOR_DIM, font=self.font_sm)

    def _flush(self):
        """Convert PIL RGB image to RGB565 bytes and send to display.

        Slice 12.1: numpy-vectorized fast path. The legacy pure-Python
        loop is still here as a fallback for environments without numpy
        (shouldn't happen on the Pi but keeping the code path for safety).
        """
        t0 = time.monotonic()
        if _HAS_NUMPY:
            buf = self._flush_numpy()
            t_conv = time.monotonic()
            self.board.draw_image(0, 0, self.W, self.H, buf)
        else:
            self._flush_python()
            t_conv = time.monotonic()
        t1 = time.monotonic()
        # Only log when meaningfully slow (>30ms) to avoid spam
        total_ms = (t1 - t0) * 1000
        if total_ms > 30:
            dbg.event("RENDER flush slow",
                      total_ms=round(total_ms, 1),
                      conv_ms=round((t_conv - t0) * 1000, 1),
                      spi_ms=round((t1 - t_conv) * 1000, 1),
                      backend="numpy" if _HAS_NUMPY else "python")

    def _flush_numpy(self):
        """Vectorized RGB888 -> RGB565 (big-endian) using numpy.
        Returns a Python list of ints (matches WhisPlay board API)."""
        rgb = np.asarray(self.img, dtype=np.uint8)  # (H, W, 3)
        r = rgb[:, :, 0].astype(np.uint16)
        g = rgb[:, :, 1].astype(np.uint16)
        b = rgb[:, :, 2].astype(np.uint16)
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        flat = rgb565.ravel()
        # Big-endian: high byte first then low byte. Two strided assigns.
        buf = np.empty(flat.size * 2, dtype=np.uint8)
        buf[0::2] = (flat >> 8).astype(np.uint8)
        buf[1::2] = (flat & 0xFF).astype(np.uint8)
        return buf.tolist()

    def _flush_python(self):
        """Legacy pure-Python conversion (slow — ~350ms on Pi Zero 2W).
        Kept as a fallback for environments without numpy."""
        pixels = self.img.tobytes()
        buf = self._buf
        idx = 0
        for i in range(0, len(pixels), 3):
            r = pixels[i]
            g = pixels[i + 1]
            b = pixels[i + 2]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf[idx] = (rgb565 >> 8) & 0xFF
            buf[idx + 1] = rgb565 & 0xFF
            idx += 2
        self.board.draw_image(0, 0, self.W, self.H, list(buf))
