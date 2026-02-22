# Cortex Core — Wearable AI Memory (Pi Zero)

The brain of the Cortex wearable system. Runs on a Raspberry Pi Zero 2 W with a WhisPlay board (display, mic, button, LED). Connects to the ESP32-S3 KeyMaster dongle via BLE, receives commands from AI agents, and stores everything in a local SQLite database.

```
AI Agent ──MCP──> cortex-mcp ──serial──> ESP32 (KeyMaster) ──BLE──> [this code] Pi Zero 2 W
                                                                     ├── SQLite DB
                                                                     ├── BLE client (bleak)
                                                                     ├── Voice STT (Vosk)
                                                                     ├── Audio recorder
                                                                     └── Display UI (PIL)
```

## Project Structure

```
cortex-core/
├── src/                    # All Python source (runs on Pi)
│   ├── main.py             # Entry point — state machine + main loop
│   ├── config.py           # All configuration constants
│   ├── ble_client.py       # BLE central client (bleak, background thread)
│   ├── cortex_protocol.py  # CMD: protocol handler + chunk reassembly
│   ├── cortex_db.py        # SQLite persistence (7 tables)
│   ├── display.py          # 240x280 ST7789 display renderer (PIL)
│   ├── recorder.py         # Audio recording via arecord
│   ├── stt.py              # Speech-to-text engine (Vosk)
│   ├── button.py           # Single-button input handler
│   ├── led.py              # RGB LED status patterns
│   ├── logger.py           # JSONL activity logger
│   ├── power.py            # WiFi power management
│   └── test_cortex.py      # Standalone tests (no hardware needed)
├── scripts/
│   ├── deploy.sh           # rsync + restart service on Pi
│   └── setup_power.sh      # One-time Pi power optimization
├── systemd/
│   └── cortex-core.service # systemd unit file
├── docs/
│   └── BLE_PROTOCOL.md     # BLE protocol specification
├── requirements.txt
└── README.md
```

## Development Workflow

**Edit locally, deploy to Pi:**

```bash
# Edit code with Claude Code (or any editor) on your PC
# Then deploy to the Pi:
bash scripts/deploy.sh

# Deploy without restarting the service:
bash scripts/deploy.sh --no-restart
```

### First-Time Setup

```bash
# 1. Clone the repo on your PC
git clone https://github.com/turfptax/cortex-core.git

# 2. Install on Pi (creates dirs, copies code, installs + starts service)
bash scripts/deploy.sh --install

# 3. Install Python deps on Pi
ssh turfptax@10.0.0.132 "pip install -r ~/cortex-core/requirements.txt"
```

### Checking Logs

```bash
# Live service logs
ssh turfptax@10.0.0.132 "sudo journalctl -u cortex-core -f"

# Recent errors only
ssh turfptax@10.0.0.132 "sudo journalctl -u cortex-core --no-pager -p err"
```

### Running Tests

The test suite runs without hardware dependencies:

```bash
ssh turfptax@10.0.0.132 "cd ~/cortex-core/src && python3 test_cortex.py"
```

## Hardware

- **Board**: Pi Zero 2 W with WhisPlay HAT (ST7789 display, WM8960 codec, button, NeoPixel LED)
- **BLE**: Connects to ESP32-S3 KeyMaster dongle as BLE central
- **Storage**: SQLite database at `~/cortex.db`
- **Audio**: 16kHz mono WAV recording via `arecord`
- **STT**: Vosk offline speech recognition (push-to-talk)

## Related Repos

- [cortex](https://github.com/turfptax/cortex) — MCP server, CLI, and daemon (runs on PC)
- [esp32-keymaster](https://github.com/turfptax/esp32-keymaster) — ESP32-S3 USB-BLE bridge firmware

## License

MIT
