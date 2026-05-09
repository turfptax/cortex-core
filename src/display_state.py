"""Typed display state — replaces the untyped render dict."""

from dataclasses import dataclass, field, fields
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

    Supports dict-style .get(key, default) for backward compatibility
    with tamagotchi_display.py.
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

    # Pet
    pet_info: Optional[dict] = None
    pet_prompt: str = ""
    pet_response_text: str = ""
    pet_resp_data: Optional[dict] = None
    pet_mode: bool = False
    idle_since: float = 0

    # STT / Notes
    stt_partial: str = ""
    note_text: str = ""

    # Recording
    session_elapsed: float = 0
    segment_elapsed: float = 0
    segment_count: int = 0
    disk_used: float = 0

    # Tamagotchi care
    cleaning_interactions: list = field(default_factory=list)
    cleaning_cursor: int = 0
    cleaning_discarded: list = field(default_factory=list)

    # Menu (populated only when app_state == "MENU")
    menu_items: list = field(default_factory=list)
    menu_cursor: int = 0
    menu_breadcrumb: str = "Menu"

    # Heartbeat / Autonomous life
    thought_bubble: str = ""           # latest heartbeat thought for HOME
    is_sleeping: bool = False          # pet is in sleep mode
    is_dreaming: bool = False          # pet is dream-training
    sleep_reason: str = ""             # why pet is sleeping
    blended_energy: float = 1.0        # battery-blended energy (0-1)

    # Settings adjustment screens
    setting_name: str = ""             # "Brightness", "Volume", "Display Hz"
    setting_value: int = 0             # current value (0-100 or Hz)
    setting_min: int = 0
    setting_max: int = 100

    # Info screens
    info_title: str = ""               # screen title
    info_lines: list = field(default_factory=list)  # list of (label, value) tuples

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style access for backward compat with tamagotchi_display.py."""
        try:
            return getattr(self, key)
        except AttributeError:
            return default
