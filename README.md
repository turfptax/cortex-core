# Cortex Core — Wearable AI Memory (Pi Zero)

The brain of the Cortex wearable system. Runs on a Raspberry Pi Zero 2 W with a WhisPlay board (display, mic, button, LED). Receives commands from AI agents via WiFi HTTP (preferred) or BLE (fallback via ESP32 dongle), and stores everything in a local SQLite database.

```
AI Agent ──MCP──> cortex-mcp ──WiFi HTTP──> [this code] Pi Zero 2 W   (preferred)
                              ──serial──> ESP32 ──BLE──> [this code]   (fallback)
                                                          ├── SQLite DB (8 tables)
                                                          ├── HTTP API (port 8420)
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
│   ├── cortex_db.py        # SQLite persistence (8 tables)
│   ├── http_server.py      # HTTP API server (WiFi transport, port 8420)
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

## HTTP API (WiFi Transport)

The HTTP server (`http_server.py`) runs on port 8420 alongside BLE — both transports are active simultaneously. Bearer token authentication is auto-generated and stored at `~/cortex-http.secret`.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health check (for auto-detection) |
| POST | `/api/cmd` | Yes | Execute any CMD: protocol command |
| GET | `/files/<category>` | Yes | List files in category |
| GET | `/files/<category>/<name>` | Yes | Download a file |
| POST | `/files/uploads` | Yes | Upload file (raw body + X-Filename header) |
| DELETE | `/files/<category>/<name>` | Yes | Delete file (recordings/uploads only) |
| GET | `/files/db` | Yes | Download cortex.db snapshot |

File categories: `recordings`, `notes`, `logs`, `uploads`.

BLE auto-discovery: After connecting to the ESP32 over BLE, the Pi sends a `DISCOVER:` message containing its IP, HTTP port, and auth token to the computer. This enables automatic WiFi transport setup with no manual configuration.

## Database

SQLite database at `~/cortex.db` with WAL mode. 8 tables:

- **sessions** — AI conversation sessions (id, platform, hostname, started_at, summary)
- **notes** — Timestamped notes with tags, project, and type (note/decision/bug/reminder/idea/todo/context)
- **activities** — Program and file tracking (what was being worked on)
- **searches** — Research query history
- **projects** — Project registry (tag, status, priority, description)
- **computers** — Registered machines (hostname, OS, hardware)
- **people** — Collaborator directory
- **files** — File metadata for AI-discoverable sharing (filename, category, description, tags)

## Related Repos

- [cortex](https://github.com/turfptax/cortex) — MCP server, CLI, and daemon (runs on PC)
- [cortex-link](https://github.com/turfptax/cortex-link) — ESP32-S3 USB-BLE bridge firmware

## License

MIT
