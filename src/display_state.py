"""Typed display state — replaces the untyped render dict."""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BLEInfo:
    name: str = ""
    address: str = ""
    mtu: int = 0
    rssi: int = 0


@dataclass
class DisplayState:
    """All data the display needs to render a frame.

    Supports dict-style .get(key, default) so out-of-tree plugin
    renderers (e.g. the cortex-pet sister repo's tamagotchi_display.py,
    loaded at runtime on .25) can read optional fields safely.
    """

    # Core state
    app_state: str = "HOME"
    time_str: str = "--:--"
    note_count: int = 0
    rec_count: int = 0
    disk_free: float = 0
    remaining_hours: float = 0

    # Connectivity
    ble_connected: bool = False
    ble_info: Optional[BLEInfo] = None

    # Battery
    battery_info: Optional[dict] = None

    # Idle tracking (monotonic timestamp of last user interaction)
    idle_since: float = 0

    # STT / Notes
    stt_partial: str = ""
    note_text: str = ""

    # Recording
    session_elapsed: float = 0
    segment_elapsed: float = 0
    segment_count: int = 0
    disk_used: float = 0

    # Menu (populated only when app_state == "MENU")
    menu_items: list = field(default_factory=list)
    menu_cursor: int = 0
    menu_breadcrumb: str = "Menu"

    # Settings adjustment screens
    setting_name: str = ""             # "Brightness", "Volume", "Display Hz"
    setting_value: int = 0             # current value (0-100 or Hz)
    setting_min: int = 0
    setting_max: int = 100

    # Info screens
    info_title: str = ""               # screen title
    info_lines: list = field(default_factory=list)  # list of (label, value) tuples

    # ── Slice 12: overseer companion ────────────────────────────
    companion_status: str = ""         # "" | "armed" | "flag-recording" |
                                       # "flag-transcribing" | "flag-saved" |
                                       # "transcribing" | "asking-overseer" |
                                       # "reply-speaking" | "saving-journal" |
                                       # "saved" | "idle"
    companion_message: str = ""        # last reply / journal text to show on screen
    companion_routed: str = ""         # "journal" | "overseer-chat" | "flag-moment"
    overseer_notes_total: int = 0      # core_stats.notes_total via /plugins/overseer/status
    overseer_unread: int = 0           # overseer_db.notifications_unread
    overseer_pending: int = 0          # overseer_db.pending_interpretations_pending
    overseer_last_tick: str = ""       # last_tick_at, e.g. "2026-05-09T17:23Z"
    overseer_loop_running: bool = False
    notification_preview: str = ""     # latest unread notification preview text
    journal_queue_depth: int = 0       # local queue depth (offline degrade)
    # Slice 12.1.2 toggle-record: seconds elapsed since the user pressed
    # to start the active recording. Updated every render frame from
    # StateManager._oc_record_start_mono. Drives the on-screen timer.
    recording_elapsed_s: float = 0     # 0 when not recording

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style access for out-of-tree plugin renderers (cortex-pet)."""
        try:
            return getattr(self, key)
        except AttributeError:
            return default
