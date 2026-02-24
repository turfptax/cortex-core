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
DISPLAY_TIMEOUT_S = 30  # backlight off after this many seconds of inactivity
BACKLIGHT_BRIGHTNESS = 50  # 0-100
DISPLAY_UPDATE_HZ = 2  # frames per second

# Fonts
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_LARGE = 18
FONT_MEDIUM = 14
FONT_SMALL = 11

# Colors (RGB tuples for PIL)
COLOR_BG = (0, 0, 0)
COLOR_TEXT = (255, 255, 255)
COLOR_DIM = (128, 128, 128)
COLOR_RED = (255, 0, 0)
COLOR_GREEN = (0, 200, 0)
COLOR_YELLOW = (255, 180, 0)
COLOR_BLUE = (0, 100, 255)
COLOR_BAR_BG = (40, 40, 40)

# Button timing (milliseconds)
SHORT_PRESS_MAX_MS = 500
LONG_PRESS_MS = 1500
SHUTDOWN_PRESS_MS = 5000

# Byte rate for disk capacity calculation
BYTE_RATE = SAMPLE_RATE * CHANNELS * 2  # 16-bit = 2 bytes per sample

# STT / Vosk
VOSK_MODEL_PATH = os.path.join(HOME, "vosk-model-small")
STT_SAMPLE_RATE = 16000
STT_CHUNK_SIZE = 4000        # bytes per read (~125ms of audio)
STT_LISTEN_TIMEOUT_S = 5     # silence timeout in listening mode
STT_NOTE_SILENCE_S = 3       # silence timeout in note-taking mode
NOTES_DIR = os.path.join(HOME, "notes")

# STT Colors
COLOR_CYAN = (0, 200, 200)
COLOR_CYAN_DIM = (0, 60, 60)

# BLE / ESP32 KeyMaster
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
HTTP_TOKEN_PATH = os.path.join(HOME, "cortex-http.secret")
UPLOADS_DIR = os.path.join(HOME, "uploads")
