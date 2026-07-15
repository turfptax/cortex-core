# Cortex Core — AI Agent Guide

> **THIS FILE IS COMMITTED AND PUBLIC.** Never put real personal data, client or
> employer names, secrets, or private-life details in this file or any tracked
> file. All instance-specific values live in gitignored config (see `SECURITY.md`
> and the gitignored `memory/core/*.md`). When you need real context, read those
> gitignored files; do not bake their contents into tracked code or docs.

If you're an AI agent starting work in this repo, read this first.

## What this repo is

Cortex Core is the Pi-side of [Cortex](https://github.com/turfptax/cortex-core), Tory Moghadam's personal AI memory system. The Python runtime lives in `src/`. The interpretive memory layer (overseer plugin) lives in `plugins/overseer/`.

Cortex serves **three functions**:
- **F1** — Serve digital data to future AIs for context (primary mission; the reason the system exists)
- **F2** — Tory's personal data-org software (Hub UI is on the cortex-desktop side, but the storage + interpretive layer lives here)
- **F3** — R&D testbed for AI maturation (sibling dispatch, B/C agents, dialectic, voice mode)

The mission was articulated by Tory in his own words 2026-05-26: *"to save and process all my digital data for future serving to new AIs for context."*

## Boot sequence for new AIs (read in this order)

### 1. The agentic triangle — three identity files

`memory/core/` holds three identity files defining the agents in the room:

| File | What |
|---|---|
| `memory/core/USER.md` | Who Tory is. **Gitignored** — exists locally on this machine. If you can read it, do. If not, read `example.USER.md` for the structure + ask Tory for the actual file content. |
| `memory/core/OVERSEER.md` | The memory-upkeep agent's identity. Gitignored. Same fallback. |
| `memory/core/APP.md` | Cortex's self-description. Gitignored. Same fallback. |
| `memory/core/example.*.md` | Public templates with placeholders — always tracked. |
| `memory/core/README.md` | Explains the gitignore pattern. |

**The gitignore pattern**:
```
memory/core/*.md
!memory/core/example.*.md
!memory/core/README.md
```

Verify with `git check-ignore -v memory/core/USER.md` — it should be ignored. Personal info never reaches the public repo.

### 2. The portable dossier (if you can't connect to Cortex)

If MCP access to the corpus isn't available, `dossier/Tory_Moghadam_Dossier_v3_2026-05-27.docx` is the portable identity reference. Also gitignored. Use `python-docx` or pandoc to extract.

### 3. Architectural seeds (the design)

Lives in the user's CCD memory directory at `C:\Users\User\.claude\projects\C--dev-ttx-Cortex\memory\` (Windows side; not in this repo). If you have access, read in this order:

1. `three_functions_of_cortex_design_seed.md` — the F1/F2/F3 frame
2. `three_layer_architecture_design_seed.md` — abstractions → gists → raw, pulled top-down
3. `vault_as_primary_surface_design_seed.md` — vault is the canonical reader corpus
4. `mcp_surface_redesign_seed.md` — 40+ tools → 6 tools

### 4. The handoff document

`memory/session_2026-05-27_l99_reframe_complete.md` (in the user's CCD memory) is the full session log of the major architectural reframe from May 26-27, 2026. Read it to understand what happened, what shipped, and what's pending.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ LAYER 1: ABSTRACTIONS  ← external AIs pull FIRST                │
│   summaries_theme, patterns, drift_observations,                │
│   open_questions, overseer_people, temporal_narratives,         │
│   summaries_episode, known_blindspots, future_overseer_notes,   │
│   project_summaries                                              │
└─────────────────────────────────────────────────────────────────┘
                              │ link
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ LAYER 2: GISTS  ← drilled when abstraction insufficient         │
│   summaries_gist (3,450+ rows)                                  │
│   Gist PROMPTS are first-class artifacts that evolve.           │
└─────────────────────────────────────────────────────────────────┘
                              │ link
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ LAYER 3: RAW DATA  ← drilled rarely; sensitivity-gated          │
│   imported_sessions + .jsonl files                               │
│   Slice 13 sensitivity rules apply at pull/render time.         │
└─────────────────────────────────────────────────────────────────┘
```

## Key directories

```
cortex-core/
├── src/                              # Pi runtime
│   ├── main.py
│   ├── cortex_db.py
│   ├── http_server.py
│   ├── plugin_api.py
│   └── ...
├── plugins/
│   ├── overseer/                     # The memory-upkeep agent
│   │   ├── __init__.py               # HTTP routes
│   │   ├── overseer_db.py            # Schema + helpers
│   │   ├── loop.py                   # The background loop
│   │   ├── chat.py                   # Overseer chat handler
│   │   ├── journal.py                # Journal step
│   │   ├── temporal_narrative.py     # Daily/weekly/monthly/yearly synth
│   │   ├── temporal.py               # Period bounds + label parsing
│   │   ├── category_classifier.py    # Flash classifier for web-AI sessions
│   │   ├── b_agents.py               # Category B stateless audits
│   │   ├── chat_tools.py             # Tool palette for journal + chat
│   │   ├── llm_router.py             # OpenRouter + on-device chain
│   │   ├── git_ingest.py             # GitHub repo ingest
│   │   ├── prompts.py                # Prompt templates
│   │   ├── plugin.toml               # Model overrides + budget config
│   │   └── data/
│   │       ├── overseer.db           # Source of truth
│   │       └── imports/              # Raw .jsonl files
│   # Pet plugin lives in the cortex-pet sister repo and is loaded
│   # at runtime on .25 from there. See github.com/turfptax/cortex-pet
├── scripts/
│   ├── backfill_session_stats.py
│   └── migrate_timestamps_to_local.py
├── memory/                           # AI-readable architecture
│   └── core/                         # The three core identity files
└── dossier/                          # Portable identity dossier (gitignored)
```

## Current state (2026-05-27)

> **STALE SNAPSHOT (kept for history).** This section is from 2026-05-27 and
> most of it is obsolete: MCP corpus access was fixed long ago, the vault
> shipped, sqlite-vec semantic recall is live, and the Phase 1-3 items below
> are done. For the real current state read the memory index at
> `C:\Users\User\.claude\projects\C--dev-ttx-Cortex\memory\MEMORY.md` (or ask
> the overseer). Corpus is now ~3,800+ gists and the interpretive layer is
> served to external AIs over MCP + the gateway.

- **3,450 gists** + **374 overseer journal entries** + **214 temporal narratives** + ~90 patterns/drift/blindspots
- **Backlog**: 5 sessions left to summarize (down from 1,129 this morning)
- **MCP corpus access**: BROKEN. The notes_search tool reads only the `notes` table which has 0 rows. The interpretive layer is sealed off from external AIs. Phase 1 fix (substring search tool) is the priority next move.
- **Vault**: Pending. Sibling Task #19 is in flight to design the structure.
- **Last journal entry**: #375 at 2026-05-26 10:59 CDT (overseer reframed its role this session).

## What's paused (don't resume without explicit go from Tory)

- Project Missions slice (waiting on vector index)
- Insight scan auto-loop (queue accumulates without promotion UX)
- Distill corrections auto-loop (same shape)
- B-agent C-graduations
- Project narratives loop step (vault will render these once it ships)

## Outstanding work

### Phase 1 (~2 days, next move)
- Substring `cortex_search` MCP tool over interpretive tables
- Expose 3 sealed tables (`patterns`, `drift_observations`, `future_overseer_notes`) via existing detail-token pattern
- Claude Desktop importer scaffold
- Add `pull_events` + `gist_prompts` tables per three-layer architecture seed

### Phase 2 (~2-3 weeks)
- Vault generator (from sibling Task #19 design when it lands)
- Vault-aware MCP tools

### Phase 3 (~1 month)
- sqlite-vec + local embedding model for semantic recall

### Phase 4 (parallel)
- Slack/Teams selective import
- Outlook calendar sync
- GitHub repo expansion

## Working style (calibration from Tory)

- **Precision over speed.** Flag uncertainty rather than confabulate. An over-confident inference about a person in a prior draft turned out wrong; that's the failure mode he doesn't want.
- **Honest disagreement welcome.** Push back rather than agree to avoid friction.
- **Sensitive topics handled carefully**: some of Tory's personal history and confidential client work must stay out of tracked/public files. Keep specifics in the gitignored identity files (memory/core/) and treat anything flagged confidential (Slice 13) with care.
- **Format**: concise with concrete detail; tables for comparisons; real numbers preferred.
- **Tory understates his own strengths** — calibrate when he describes his own capabilities.

## Deployment

The Pi at `turfptax@10.0.0.25` runs cortex-core. After code changes:

```bash
# From this Windows machine
scp cortex-core/src/*.py turfptax@10.0.0.25:/tmp/
ssh turfptax@10.0.0.25 "sudo mv /tmp/*.py /home/turfptax/cortex-core/src/ && sudo chown root:root /home/turfptax/cortex-core/src/*.py && sudo systemctl restart cortex-core"
```

Plugin updates: replace `src/` with `plugins/overseer/`.

## Harness map (update with every feature)

`memory/HARNESS_MAP.md` is the single succinct map of every screen and
feature across the whole harness (Hub, phone, Pi). The overseer reads
it via the `get_harness_map` chat tool and `GET /plugins/overseer/harness-map`,
and it grounds "Discuss with Overseer" escalations from any surface.
**When you ship a user-facing feature in ANY Cortex repo, update this
file and redeploy it to the Pi.** Keep entries to 1-2 lines; no
personal data (this repo is public).

## Git hygiene

- Always stage explicit files. Don't `git add -A` from the repo root.
- The cortex-core repo on `.25` is owned by root for the service runtime. Pushing from .25 requires SSH key setup that isn't there — push from this Windows side instead.
- Personal identity files in `memory/core/` and `dossier/` are gitignored. Verify with `git check-ignore -v <path>` before any commit involving them.

## Related repos

- [cortex-desktop](https://github.com/turfptax/cortex-desktop) — Windows Hub UI + MCP server + training pipeline
- [cortex-link](https://github.com/turfptax/cortex-link) — ESP32-S3 BLE bridge
- [cortex-pet](https://github.com/turfptax/cortex-pet) — extracted pet plugin; loaded on production .25 at runtime from this sibling repo
- [cortex-pet-training](https://github.com/turfptax/cortex-pet-training) — training scripts (consolidating into cortex_train)

## When in doubt

- Read `memory/core/USER.md` for who you're serving.
- Read `memory/core/OVERSEER.md` for the agent already running here.
- Read `memory/core/APP.md` for the system architecture.
- Hit `overseer_chat` MCP tool for deep questions — overseer has the May 26-27 reframes locked in working memory.
- Read the session handoff doc `memory/session_2026-05-27_l99_reframe_complete.md` in the user's CCD memory directory if you need the full context of recent decisions.
