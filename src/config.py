"""Configuration constants for the wearable audio recorder."""

import os

# Audio
AUDIO_DEVICE = "plughw:wm8960soundcard"
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_FORMAT = "S16_LE"
SEGMENT_SECONDS = 900  # 15 minutes

# Paths — derived from this file's location so it works when running as root
# config.py lives at ~/cortex-core/src/config.py
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.dirname(os.path.dirname(_THIS_DIR))  # src -> cortex-core -> home
APP_DIR = _THIS_DIR
RECORDING_DIR = os.path.join(HOME, "recordings")
LOG_DIR = os.path.join(HOME, "logs")
WHISPLAY_DRIVER = os.path.join(HOME, "Whisplay", "Driver")

# Display
DISPLAY_WIDTH = 240
DISPLAY_HEIGHT = 280
DISPLAY_TIMEOUT_S = 60  # backlight off after this many seconds of inactivity (Slice 12: bumped from 30 — companion flows can run 5-15s)
BACKLIGHT_BRIGHTNESS = 100  # 0-100
DISPLAY_UPDATE_HZ = 8  # frames per second (Slice 12: bumped from 2 — button-press feedback was lagging up to 500ms)

# Fonts
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_LARGE = 18
FONT_MEDIUM = 14
FONT_SMALL = 11

# ── Cyberpunk Color Palette (RGB tuples for PIL) ──────────────
# Base colors
COLOR_BG = (8, 8, 20)                 # Deep space blue-black
COLOR_TEXT = (220, 230, 240)           # Cool white (slight blue tint)
COLOR_DIM = (60, 70, 90)              # Muted steel blue
COLOR_RED = (255, 40, 80)             # Neon red-pink
COLOR_GREEN = (0, 255, 140)           # Neon green
COLOR_YELLOW = (255, 220, 0)          # Neon amber
COLOR_BLUE = (0, 120, 255)            # Electric blue
COLOR_BAR_BG = (16, 16, 32)           # Dark bar trough

# Neon accents
COLOR_MAGENTA = (255, 0, 200)         # Neon magenta (secondary accent)
COLOR_MAGENTA_DIM = (80, 0, 60)       # Dimmed magenta

# UI chrome
COLOR_SEPARATOR = (20, 30, 50)        # Separator lines
COLOR_SPEECH_BORDER = (0, 255, 255)   # Neon border for speech bubbles

# Circuit trace background
COLOR_CIRCUIT_PRIMARY = (12, 20, 35)  # Very subtle trace lines
COLOR_CIRCUIT_NODE = (16, 28, 45)     # Junction dots (slightly brighter)

# Button timing (milliseconds)
# Slice 12.1 (2026-05-09): SHORT_PRESS_MAX_MS doubles as the
# "you've crossed into hold-mode" threshold for companion-mode UX.
# Was 500ms; lowered to 350 because 500 is long enough that users
# overshoot taps. Also raised SHUTDOWN_PRESS_MS from 5000 -> 7000
# so a moderately-long hold-record (e.g. 5-6 sec voice memo) doesn't
# trip shutdown. LONG_PRESS_MS is now legacy — kept for the gamepad
# action handlers that still call handle_long_press, but the physical
# button no longer wires to it (button.py uses hold_threshold/release).
SHORT_PRESS_MAX_MS = 350
LONG_PRESS_MS = 1500
SHUTDOWN_PRESS_MS = 7000

# Byte rate for disk capacity calculation
BYTE_RATE = SAMPLE_RATE * CHANNELS * 2  # 16-bit = 2 bytes per sample

# STT / Vosk
VOSK_MODEL_PATH = os.path.join(HOME, "vosk-model-small")
STT_SAMPLE_RATE = 16000
STT_CHUNK_SIZE = 4000        # bytes per read (~125ms of audio)
STT_LISTEN_TIMEOUT_S = 5     # silence timeout in listening mode
STT_NOTE_SILENCE_S = 3       # silence timeout in note-taking mode
NOTES_DIR = os.path.join(HOME, "notes")

# STT / Primary accent colors
COLOR_CYAN = (0, 255, 255)            # Neon cyan (primary accent)
COLOR_CYAN_DIM = (0, 60, 80)          # Dimmed cyan

# BLE / ESP32 KeyMaster
BLE_ENABLED = True                      # Orange Pi Zero 2W has BT hardware
BLE_DEVICE_NAME = "KeyMaster"
BLE_SERVICE_UUID = "a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e50"
BLE_TX_UUID = "a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e51"  # ESP32 -> Pi
BLE_RX_UUID = "a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e52"  # Pi -> ESP32
BLE_RECONNECT_INTERVAL_S = 5
BLE_MAX_MESSAGE_LEN = 512

# Cortex Database
CORTEX_DB_PATH = os.path.join(HOME, "cortex.db")
CORTEX_CHUNK_TIMEOUT_S = 30.0

# HTTP API Server (WiFi transport)
HTTP_ENABLED = True
HTTP_PORT = 8420
HTTP_USERNAME = "cortex"
HTTP_PASSWORD = "cortex"
UPLOADS_DIR = os.path.join(HOME, "uploads")

# Pet plugin source was extracted to the cortex-pet sister repo
# (Slice 11, 2026-05-09). The plugin is still loaded at runtime on
# production .25 from that sibling repo.
# Constants previously here (PET_*, HEARTBEAT_*, DREAM_*, COMA_*, SPRITE_*,
# COLOR_PET_*, COLOR_VITAL_*, COLOR_CURSOR/MENU_BG/HIGHLIGHT/SPEECH_BG/
# XP_BAR/XP_BAR_BG, BATTERY_ENERGY_WEIGHT, INFERENCE_ENERGY_WEIGHT,
# BATTERY_DREAM_MIN_PCT) live in cortex-pet's pet_config.py.
# See https://github.com/turfptax/cortex-pet

# Gamepad (8BitDo Micro via evdev)
# Slice 12.1.2 (2026-05-09): GAMEPAD_ENABLED is now env-overridable.
# On .132 (Pi Zero 2W companion), the gamepad isn't used (single board
# button is the input device). The bluetoothctl scan inside
# gamepad.poll() blocks the main loop for 5-13 seconds per attempt,
# which made the on-screen recording timer update once every ~10s
# instead of 8x per second. Set CORTEX_GAMEPAD_ENABLED=0 in the
# systemd unit on .132 to skip the scan entirely.
GAMEPAD_ENABLED = os.environ.get("CORTEX_GAMEPAD_ENABLED", "1") != "0"
GAMEPAD_DEVICE_NAME = "8BitDo"
GAMEPAD_MAC = "E4:17:D8:68:7C:ED"  # 8BitDo Micro in Android/D-input mode
GAMEPAD_REPEAT_DELAY_S = 0.4   # seconds before auto-repeat starts
GAMEPAD_REPEAT_RATE_S = 0.15   # seconds between auto-repeat events

# Games (slice 2c2 may extract Pong into its own plugin)
GAMES_ENABLED = True
GAME_FPS = 15  # frame rate during gameplay (vs DISPLAY_UPDATE_HZ normally)
PONG_MODEL_PATH = os.path.join(HOME, "models", "pong_qtable.json")

# ── Battery (PiSugar 3) ────────────────────────────────────────
BATTERY_ENABLED = True
BATTERY_POLL_INTERVAL_S = 30       # seconds between I2C reads
BATTERY_I2C_BUS = None             # None = auto-detect

# Real battery thresholds — hardware behavior, not pet behavior.
# Pet's blend weights live in the cortex-pet sister repo's pet_config.py.
BATTERY_FORCE_SLEEP_PCT = 15       # force sleep below this battery %
BATTERY_CRITICAL_PCT = 5           # graceful shutdown below this

# ── Sound System ────────────────────────────────────────────────
SOUND_ENABLED = True
SOUND_DEVICE = "plughw:wm8960soundcard"
SOUND_DIR = os.path.join(APP_DIR, "assets", "sounds")

# ── Plugin System (v0 scaffolding) ─────────────────────────────────
PLUGINS_ENABLED = True             # set False to skip plugin discovery at boot
