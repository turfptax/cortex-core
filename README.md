# Cortex Core — Wearable AI Companion

The brain of the Cortex wearable system. Runs on an **Orange Pi Zero 2W** (Allwinner H616, 2GB RAM) with a WhisPlay board (display, speaker, gamepad). Receives commands from AI agents via WiFi HTTP (preferred) or BLE (fallback via ESP32 dongle), and stores everything in a local SQLite database.

```
AI Agent ──MCP──> cortex-mcp ──WiFi HTTP──> [this code] Orange Pi Zero 2W  (preferred)
                              ──serial──> ESP32 ──BLE──> [this code]        (fallback)
                                                          ├── SQLite DB (10 tables)
                                                          ├── HTTP API (port 8420)
                                                          ├── BLE client (bleak)
                                                          ├── Pet Engine (llama-server, Qwen3.5-0.8B)
                                                          ├── Voice STT (Vosk)
                                                          ├── Audio recorder
                                                          └── Display UI (PIL, 240x280 ST7789)
```

## Project Structure

```
cortex-core/
├── src/                    # Core Python source (runtime + plugin host)
│   ├── main.py             # Entry point — state machine + main loop
│   ├── config.py           # All configuration constants
│   ├── ble_client.py       # BLE central client (bleak, background thread)
│   ├── cortex_protocol.py  # CMD: protocol handler + chunk reassembly
│   ├── cortex_db.py        # SQLite persistence (10 tables)
│   ├── http_server.py      # HTTP API server (WiFi transport, port 8420)
│   ├── plugin_api.py       # Plugin host: discovery, lifecycle, route registry
│   ├── display.py          # 240x280 ST7789 display renderer (PIL)
│   ├── recorder.py         # Audio recording via arecord
│   ├── stt.py              # Speech-to-text engine (Vosk)
│   ├── button.py           # Single-button input handler
│   ├── led.py              # RGB LED status patterns
│   ├── logger.py           # JSONL activity logger
│   └── power.py            # WiFi power management
├── plugins/                # First-class plugins (auto-discovered, hot-loaded)
│   ├── pet/                # Tamagotchi-style pet (was src/pet.py)
│   │   ├── __init__.py
│   │   ├── pet_engine.py
│   │   ├── tamagotchi_display.py
│   │   ├── plugin.toml
│   │   └── data/           # Per-plugin SQLite + assets
│   └── overseer/           # Memory-upkeep agent (Slice 3 + 4)
│       ├── __init__.py             # Plugin lifecycle + ~30 HTTP routes
│       ├── overseer_db.py          # Schema + CRUD (15+ tables)
│       ├── llm_router.py           # OpenRouter / LM Studio routing
│       ├── core_memory_ro.py       # Read-only access to cortex.db
│       ├── loop.py                 # Background tick loop (8 steps)
│       ├── claude_jsonl.py         # Claude Code .jsonl parser + extended stats
│       ├── chat.py                 # Overseer chat (Opus 4.7 default)
│       ├── insight_scan.py         # Pattern + drift detection
│       ├── distill_corrections.py  # Slice 3i: corrections → blindspots
│       ├── blindspots.py           # Meta-honesty layer (Slice 3f.5)
│       ├── dialectic.py            # Paired Opus + Gemma generation (Slice 3f)
│       ├── journal.py              # Append-only overseer reflection
│       ├── notifications.py        # Bell rules engine
│       ├── automation_rollup.py    # Per-project daily rollups (Slice 3e)
│       ├── question_routing.py     # Route gists → open_questions
│       ├── project_summary.py      # Slice 4 CP1a: per-project rollup data
│       ├── project_narrative.py    # Slice 4 CP1b: Sonnet narrative generator
│       ├── pricing.py              # Anthropic price table (as_of: 2026-05-02)
│       ├── temporal.py             # Slice 5: local-TZ helpers, 22:00 trigger
│       ├── temporal_narrative.py   # Slice 5: daily/weekly/monthly Sonnet rollups
│       ├── detail.py               # Token-based drill-down (Slice 3g)
│       └── data/                   # overseer.db
├── scripts/
│   ├── deploy.sh                       # scp + restart service on Pi
│   ├── setup_llama_server.sh           # Build llama-server (-j1 for 2GB boards)
│   ├── setup_pet.sh                    # Install llama-cpp-python + model
│   ├── setup_power.sh                  # Pi power optimization
│   └── backfill_session_stats.py       # Slice 4: one-shot per-session enrichment
├── systemd/
│   └── cortex-core.service             # systemd unit file
├── llama-server.service                # systemd unit for LLM inference
├── docs/
│   ├── BLE_PROTOCOL.md
│   ├── 8BITDO_MICRO_SETUP.md
│   ├── UX_DESIGN_BRIEF.md
│   └── WM8960_ORANGE_PI_KNOWN_ISSUES.md
├── requirements.txt
└── README.md
```

## Plugins

The runtime hosts plugins under `plugins/`. Each plugin owns:
- A `plugin.toml` manifest (name, version, dependencies, optional `[llm]` config)
- A subclass of `plugin_api.Plugin` with an `on_load(self)` lifecycle hook
- Its own SQLite at `plugin_data/` (the runtime gives a typed `db` handle)
- A `routes` list — `Route("GET"|"POST", "/path", handler)` mounted under `/plugins/<name>/...`

Two ship today:

- **pet** — Tamagotchi engine (vitals, mood, evolution, LLM chat, display rendering, gamepad input)
- **overseer** — Memory-upkeep agent. Reads notes / sessions / imported Claude Code conversations and produces interpretive layers via OpenRouter (Opus 4.7 + Sonnet 4.6 dialectic). Background loop with 9 tick steps as of v0.17 cycle (Slice 5 added the temporal cadence step). Slice 3 + Slice 4 + Slice 5 — full feature set documented in [cortex-desktop/CLAUDE.md](https://github.com/turfptax/cortex-desktop/blob/master/CLAUDE.md#overseer-slice-3--4--5--added-in-v013017).
  **Locked principle (Slice 5):** the Overseer stays a quiet, lightweight memory layer. Captures, surfaces, connects. Not a journaling app or life coach.

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
ssh turfptax@10.0.0.25 "pip install -r ~/cortex-core/requirements.txt"
```

### Checking Logs

```bash
# Live service logs
ssh turfptax@10.0.0.25 "sudo journalctl -u cortex-core -f"

# Recent errors only
ssh turfptax@10.0.0.25 "sudo journalctl -u cortex-core --no-pager -p err"
```

### Running Tests

The test suite runs without hardware dependencies:

```bash
ssh turfptax@10.0.0.25 "cd ~/cortex-core/src && python3 test_cortex.py"
```

## Hardware

- **Board**: Orange Pi Zero 2W (Allwinner H616, 2GB RAM) with WhisPlay HAT (ST7789 display, WM8960 codec, gamepad)
- **Gamepad**: 8BitDo Micro — Bluetooth auto-pairing via evdev
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

## Pet Engine

An AI Tamagotchi running a small LLM (Qwen3.5-0.8B, Q4_K_M quantized, ~533 MB) locally on the Pi via `llama-server` (HTTP backend). The pet evolves through interaction stages, has a mood/vitals system, autonomous heartbeat reflections, and dream-cycle training.

### llama-server Setup

The pet's brain runs as a separate `llama-server` systemd service. **Always build with `-j1` on 2GB boards** — using `-j4` will OOM and crash the system.

```bash
# Full setup: build llama-server + download model + install service (~25 min)
# Run this ON the Pi (or via SSH):
bash scripts/setup_llama_server.sh

# Or step by step:
bash scripts/setup_llama_server.sh --build-only  # build llama-server (~20-30 min)
bash scripts/setup_llama_server.sh --model-only   # download Qwen3.5-0.8B (~533 MB)
bash scripts/setup_llama_server.sh --cleanup       # remove build dir (~1.5 GB)
```

The build uses static linking (`-DBUILD_SHARED_LIBS=OFF`) so there are no shared library dependencies. The resulting binary is fully self-contained.

**⚠️ IMPORTANT**: Never compile on 2GB Pi boards with more than `-j1`. The `-j4` flag will exhaust RAM, trigger OOM, and freeze the board requiring a power cycle.

### Pet Setup

```bash
# Deploy code + restart service
bash scripts/deploy.sh

# Or via deploy script with pet setup:
bash scripts/deploy.sh --pet-setup
```

Set `PET_ENABLED=False` in `config.py` to disable the pet entirely.

### Voice Interaction

Say **"pet"** followed by a prompt (e.g., "pet how are you"), or say just **"pet"** to enter pet-listening mode where everything spoken becomes the prompt.

### Evolution Stages

| Stage | Name | Interactions | Behavior |
|-------|------|-------------|----------|
| 0 | Primordial | 0-49 | Very short phrases (1-5 words), curious but confused |
| 1 | Babbling | 50-199 | Short sentences (5-15 words), repeats words, excited |
| 2 | Echoing | 200-999 | Simple conversations (1-2 sentences), developing personality |
| 3 | Responding | 1000-4999 | Real conversations (1-3 sentences), remembers things |
| 4 | Conversing | 5000+ | Mature companion (1-4 sentences), thoughtful and creative |

### Mood System

Mood is a rolling average of sentiment from the last 20 interactions. Sentiment uses weighted keyword scoring with negation handling ("not good" → negative), intensity modifiers ("very", "extremely"), and punctuation cues. Mood adjusts the LLM's generation temperature (kind = more coherent, mean = more cautious) and response length.

| Mood | Score Range | Effect |
|------|------------|--------|
| Happy | > 0.4 | Lower temperature, longer responses |
| Content | 0.15 - 0.4 | Slightly lower temperature |
| Neutral | -0.15 - 0.15 | Default behavior |
| Uneasy | -0.4 - -0.15 | Higher temperature, shorter responses |
| Sad | < -0.4 | Highest temperature, shortest responses |

### Protocol Commands

| Command | Payload | Response |
|---------|---------|----------|
| `pet_ask` | `{"prompt": "hello"}` | `ACK:pet_ask:<id>` (response arrives async) |
| `pet_response` | `{}` | `RSP:pet_response:[...]` (poll for completed responses) |
| `pet_status` | `{}` | `RSP:pet_status:{...}` (stage, mood, stats) |
| `pet_mood` | `{}` | `RSP:pet_mood:{...}` (detailed mood info) |
| `pet_history` | `{"limit": 10}` | `RSP:pet_history:[...]` (recent interactions) |
| `pet_analytics` | `{"days": 7}` | `RSP:pet_analytics:{...}` (mood trends, performance, stage progression) |

### Display States

- **PET_ASKING** — Shown while the pet is thinking. Displays the user's prompt and animated "Thinking..." indicator.
- **PET_RESPONSE** — Shows the pet's response text, inference stats, and mood/stage. Auto-dismisses after 15 seconds.
- **NOTE_TAKING (pet mode)** — When capturing a pet prompt via voice, shows "PET" badge and "Ask your pet..." placeholder.

## Shell Exec

Execute shell commands on the Pi remotely via the protocol. Useful for deploying, debugging, and managing the Pi through the MCP bridge.

| Command | Payload | Response |
|---------|---------|----------|
| `shell_exec` | `{"command": "ls -la", "timeout": 30, "cwd": "/home/turfptax"}` | `RSP:shell_exec:{"exit_code":0,"stdout":"...","stderr":"..."}` |

- **timeout**: Max seconds (default 30, max 120)
- **max_output**: Max chars per stream (default 10000, max 50000)
- **cwd**: Working directory (defaults to home)

## Database

Cortex stores state in two SQLite files:

- **`~/cortex.db`** (user-owned) — core memory the user authored: sessions, notes, activities, searches, projects, computers, people, files.
- **`~/cortex-core/plugins/overseer/data/overseer.db`** (root-owned, plugin-managed) — the overseer plugin's own working memory: imported AI conversation history (ChatGPT, Claude Code, Grok), gist + temporal summaries, the overseer's journal, dialectic questions, biometric streams, phone/voicemail logs.

A full walkthrough — including the source taxonomy (which `source=` values exist for ChatGPT vs Claude Code vs Twitter vs Grok), the per-conversation `.jsonl` import pattern, and a recipe for adding a new data source — lives in **[docs/DATA_ARCHITECTURE.md](docs/DATA_ARCHITECTURE.md)**. Read that first if you're forking cortex-core to import your own data.

### `cortex.db` tables (user-owned, ~/cortex.db)

- **sessions** — AI conversation sessions (id, platform, hostname, started_at, summary)
- **notes** — Timestamped notes with tags, project, and type (note/decision/bug/reminder/idea/todo/context/tweet/tweet-reply/tweet-retweet/voice)
- **activities** — Program and file tracking (what was being worked on)
- **searches** — Research query history
- **projects** — Project registry (tag, status, priority, description)
- **computers** — Registered machines (hostname, OS, hardware)
- **people** — Collaborator directory
- **files** — File metadata for AI-discoverable sharing (filename, category, description, tags)
- **time_entries**, **organizations**, **training_examples**, **training_ledger** — supporting tables

## Related Repos

- [cortex](https://github.com/turfptax/cortex) — MCP server, CLI, and daemon (runs on PC)
- [cortex-link](https://github.com/turfptax/cortex-link) — ESP32-S3 USB-BLE bridge firmware

## License

MIT
