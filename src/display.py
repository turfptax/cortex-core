"""Display UI renderer for the 240x280 ST7789P3 via WhisPlay driver."""

from PIL import Image, ImageDraw, ImageFont

from config import (
    DISPLAY_WIDTH, DISPLAY_HEIGHT, SEGMENT_SECONDS,
    FONT_PATH, FONT_PATH_REGULAR, FONT_LARGE, FONT_MEDIUM, FONT_SMALL,
    COLOR_BG, COLOR_TEXT, COLOR_DIM, COLOR_RED, COLOR_GREEN,
    COLOR_YELLOW, COLOR_BLUE, COLOR_BAR_BG, COLOR_CYAN, COLOR_CYAN_DIM,
)


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

        Dispatches to the appropriate screen renderer based on app_state.
        """
        self.draw.rectangle([0, 0, self.W, self.H], fill=COLOR_BG)

        app = state.get("app_state", "STT_IDLE")
        if app == "STT_IDLE":
            self._render_stt_idle(state)
        elif app == "STT_LISTENING":
            self._render_stt_listening(state)
        elif app == "NOTE_TAKING":
            self._render_note_taking(state)
        elif app in ("RECORDING", "PAUSED", "IDLE"):
            # Existing recording UI
            self._draw_status_bar(state)
            self._draw_segment_info(state)
            self._draw_progress_bar(state)
            self._draw_session_stats(state)
            self._draw_footer(state)

        self._flush()

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
        y += 20
        self.draw.text((14, y), "  audio continuously", fill=COLOR_CYAN, font=self.font_sm)

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
        """Convert PIL RGB image to RGB565 bytes and send to display."""
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
