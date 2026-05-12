# Data Architecture

How cortex-core stores what it knows, and where third-party data
(ChatGPT, Claude Code, Grok, Twitter, etc.) lands when imported.

Reading order: this doc → the source code referenced in each section.
Privacy section is intentionally at the top because it's the question
most people ask first.

---

## Privacy boundary

**All raw data stays on the device.** That's the design rule everything
below derives from.

| What lives on the device (local SQLite) | What goes to the cloud |
|---|---|
| Every note, journal entry, transcript, tweet | LLM prompts for summarization (Sonnet / Haiku / local Qwen) |
| Every imported AI conversation (full text) | Text-to-speech for the overseer's spoken replies (ElevenLabs, optional) |
| Every recording on disk (wav) | Speech-to-text for voice journals (Groq Whisper, optional; falls back to local Vosk) |
| Every imported person, contact, phone log | Nothing else by default |
| Every body metric, voicemail, phone call | |

Cloud calls are mediated by `plugins/overseer/llm_router.py` and the
voice runtime in `src/overseer_companion.py`. Every cloud-bound surface
has an env-var off switch. Secrets live in `~/.cortex/secrets.toml` and
are never committed (see `plugins/overseer/SECRETS.md`).

If you want a fully offline system, set `LLM_ROUTER_OFFLINE=1` and the
overseer falls back to the on-device Qwen via llama-server. Voice STT
falls back to Vosk. TTS falls back to `espeak-ng`. Everything else
already runs local.

---

## The two databases

Cortex deliberately splits state into two SQLite files, owned by
different processes:

```
~/cortex.db                                      (user-owned)
└── core memory: notes, sessions, people,
    projects, activities, time_entries, files, searches, ...

~/cortex-core/plugins/overseer/data/overseer.db  (root-owned, plugin-managed)
└── overseer-specific: imported_sessions, summaries_gist,
    phone_calls, voicemails, body_metrics, journal entries,
    dialectic_open, blindspots, notifications, ...
```

**Why split?** Two reasons:

1. **Plugin isolation.** The overseer is a plugin (`plugins/overseer/`).
   It owns its own data file. If you disable the overseer plugin, the
   core memory keeps working — sessions still record, notes still save,
   the wearable still functions. The overseer's analytical state stays
   parked in its own DB until you re-enable it.

2. **Permission model.** cortex-core runs as `root` on the Pi (for GPIO
   + audio access). User-owned `cortex.db` is reachable from any
   command-line tool running as `turfptax`. Root-owned `overseer.db`
   needs `sudo` for direct writes — which is the right friction for
   any human poking at the overseer's working memory.

You'll see this distinction throughout: if a piece of data is
**something the user authored** (a note, a session, a tweet), it
goes in `cortex.db`. If it's **something the overseer derived or
ingested for its own analysis** (an imported AI conversation, a gist
summary, a journal entry the overseer wrote about you), it goes in
`overseer.db`.

A few overseer-internal surfaces that live in `overseer.db` but
contain user-authored content: `human_journal_entries` (voice journal
entries captured via the wearable), `chat_messages` (Hub UI chat
history with the overseer). These are inputs the overseer needs in its
own analytical loop, so colocating them with the rest of its state
keeps the queries simple.

---

## Source taxonomy

Every row in `notes`, `imported_sessions`, and friends carries a
`source` column. That column is the contract: it tells you where the
data came from, which lets you filter, dedup, or audit cleanly.

### `cortex.db` → `notes.source`

| `source` | Origin | Generator | Notes |
|---|---|---|---|
| `ble` | Wearable → BLE | `src/cortex_protocol.py` | Notes typed/dictated to an AI agent that round-trip through cortex-mcp |
| `voice` | Wearable mic | `src/overseer_companion.py` | Voice journal entries captured by tapping the device button |
| `twitter` | Twitter X takeout | `imports/tweets_importer.py` | Original tweets, replies, retweets. `note_type` = `tweet`, `tweet-reply`, `tweet-retweet` |
| `google-takeout` | Google Takeout export | one-off import | Email, calendar, etc. |
| `tory_life.db` | Pre-cortex life history DB | `local_history_ai/migrate_to_overseer.py` | Bridged April 2023 → March 2026 history from a personal life-tracking SQLite |

### `cortex.db` → `notes.note_type`

A second axis on top of `source`. Tells you what *kind* of note this
is, regardless of where it came from:

`note`, `bug`, `idea`, `decision`, `reminder`, `todo`, `context`,
`tweet`, `tweet-reply`, `tweet-retweet`, `voice`.

### `overseer.db` → `imported_sessions.source`

This is where third-party AI conversation history goes. Each row is
**one full conversation**; the message bodies live in a per-row
`.jsonl` file pointed to by `source_path`.

| `source` | Origin | Format on disk |
|---|---|---|
| `claude-code` | Live Claude Code sessions | `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` ingested by overseer's background loop |
| `chatgpt` | OpenAI Data Export ZIP | One-off import: `imports/<chatgpt-importer>.py` |
| `grok-com` | grok.com Data Export ZIP | `imports/grok_com_importer.py` (this repo: `imports/`) |
| `grok-twitter` | Twitter X takeout (`data/grok-chat-item.js`) | `imports/grok_twitter_importer.py` |

The `id` column always carries the source as a prefix:
`claude-code:<uuid>`, `chatgpt:<uuid>`, `grok-com:<uuid>`,
`grok-twitter:<uuid>`. This makes dedup, source-scoped queries,
and lookups trivially scriptable.

### Other `overseer.db` surfaces

These don't use a `source` column the same way (they're not import
targets) but it's worth knowing they exist:

| Table | What's in it |
|---|---|
| `summaries_gist` | One-line summaries the overseer generates per imported session |
| `summaries_episode` / `summaries_theme` | Coarser rollups (week, month, theme) |
| `temporal_narratives` | Daily / weekly / monthly / yearly narrative entries |
| `human_journal_entries` | Voice journal entries from the wearable |
| `overseer_journal` | The overseer's own notes-to-self about you |
| `dialectic_open` | Open dialectic questions the overseer is pondering |
| `known_blindspots` | Things the overseer flagged as its own knowledge gaps |
| `pending_interpretations` | Tentative observations awaiting more evidence |
| `phone_calls`, `phone_contacts`, `voicemails` | Phone history (imported one-off from tory_life.db) |
| `body_metrics`, `body_response_events` | Biometric streams; see `POSTURE_NUMERIC_STREAMS.md` |
| `activity_events`, `activity_daily` | App-usage / activity time-series |

---

## Per-conversation `.jsonl` pattern

For AI conversation imports (`claude-code`, `chatgpt`, `grok-com`,
`grok-twitter`), the message bodies aren't crammed into a database
field — they live as one `.jsonl` file per conversation, on disk:

```
~/cortex-core/plugins/overseer/data/imports/
├── claude-code/
│   ├── 9662df6c-e8fd-4fe5-8953-40cae1fbef7d.jsonl
│   └── ...
├── chatgpt/
│   ├── 93fab458-8a5c-4e3d-a080-4b307c816acb.jsonl
│   └── ... (1,728 files)
├── grok-com/
│   ├── dd044b19-262a-49b7-8008-ad12edc64b16.jsonl   ← Slice 11 working session
│   └── ... (906 files)
└── grok-twitter/
    ├── 1734677556014551040.jsonl
    └── ... (22 files)
```

Each `.jsonl` is one JSON object per line, one line per message.
Format is identical across sources so the overseer can read any of
them with a single parser:

```jsonl
{"type": "user",      "timestamp": "ISO-8601", "sessionId": "<conv-id>", "cwd": "", "gitBranch": "", "message": {"role": "user",      "content": "..."}}
{"type": "assistant", "timestamp": "ISO-8601", "sessionId": "<conv-id>", "cwd": "", "gitBranch": "", "message": {"role": "assistant", "content": "..."}}
```

The `imported_sessions` row carries everything the overseer needs to
*decide* whether to process this session — title, message count,
duration, source account, file hash, etc. — without opening the
`.jsonl`. The `.jsonl` is only read when the overseer actually wants
to summarize the content.

**Sender normalization.** Different platforms use different sender
values (`human` / `User` / `USER`, `Agent` / `assistant` / `ASSISTANT`
/ `grok-4-auto`). Importers normalize to two canonical values:
`user` and `assistant`. The original raw value isn't preserved (it's
cosmetic; what matters is who said what).

**Dedup.** `imported_sessions.file_hash` is the sha256 of the
`.jsonl` contents. Re-running an importer with `INSERT OR IGNORE` and
the same source UUIDs is safe. If you want stronger dedup (e.g. when
re-importing with a different source UUID scheme), query by
`file_hash` before insert.

---

## Processing pipeline (after import)

Once a row lands in `imported_sessions`, the overseer's background
loop discovers it on its next tick:

```
imported_sessions
   ↓ (per session, async, rate-limited by LLM budget)
summaries_gist (1-2 sentence summary via Haiku/Sonnet)
   ↓ (per period — day/week/month/year)
temporal_narratives (human-readable narrative across all sources)
   ↓ (when patterns recur)
patterns / dialectic_open / known_blindspots
```

Tracking lives in `processed_imported_sessions` — every gist generation
records which session was processed and when. This lets the loop
resume after a restart without reprocessing.

If you import 906 new sessions at once (as we did with the grok.com
export), the loop walks through them over hours, not seconds — the
catchup logic from Slice 5.6.1 caps each tick's LLM-call budget and
walks back chronologically.

---

## Adding a new data source

Recipe, derived from the May 2026 ChatGPT + Grok + Twitter imports.
Concrete reference scripts live in this repo's `imports/` directory.

**1. Decide which table is the target.**

- "It's an AI conversation" → `imported_sessions` (+ per-row `.jsonl`)
- "It's a user-authored note / post / message" → `notes`
- "It's a person" → `people`
- "It's a time-bound event" → `activities` / `time_entries`

**2. Pick a `source` value.**

Lowercase, hyphenated, stable forever (it's how everything downstream
will filter). E.g. `linkedin`, `notion-export`, `signal-takeout`.

**3. Write a parser script that produces normalized rows.**

For AI conversations the parser must:

- Strip whatever wrapper the export uses (JS-wrapped JSON for Twitter,
  raw JSON for grok.com, ZIP-of-folders for OpenAI).
- Normalize sender values to `user` / `assistant`.
- Convert timestamps to ISO 8601.
- For each conversation: write a `.jsonl` and a metadata row.
- Compute `file_hash = sha256(jsonl_bytes)` for dedup.
- Set `id = "<source>:<conversation-uuid>"`.

**4. Always back up the target DB before inserting.**

```bash
cp ~/cortex-core/plugins/overseer/data/overseer.db \
   ~/backups/overseer.db.pre-<source>-import.$(date +%Y%m%d_%H%M%S)
```

**5. Use `INSERT OR IGNORE` keyed on `id`.**

Idempotency on re-runs. If you change the importer and want to
re-process, `DELETE FROM imported_sessions WHERE source = ?` first.

**6. Verify counts before declaring success.**

Snapshot `COUNT(*) GROUP BY source` before and after; the delta should
match the number you inserted.

**7. Let the overseer's background loop handle the rest.**

You don't need to manually trigger gist generation. The processing
pipeline picks up new rows on the next tick.

---

## Schemas (quick reference)

### `cortex.db` → `notes`

```sql
CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',          -- comma-separated; e.g. "twitter,tweet:1234,#hashtag"
    project TEXT DEFAULT '',
    note_type TEXT DEFAULT 'note',
    source TEXT DEFAULT 'ble',
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

Dedup happens via tag inspection (`WHERE tags LIKE '%tweet:<id>%'`) —
the schema has no `UNIQUE` constraint on external IDs because legacy
sources (BLE, voice) don't have them.

### `overseer.db` → `imported_sessions`

```sql
CREATE TABLE imported_sessions (
    id TEXT PRIMARY KEY,                   -- "<source>:<uuid>"
    source TEXT NOT NULL,
    source_path TEXT NOT NULL,             -- absolute path to .jsonl on Pi
    project TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    git_branch TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    ended_at TEXT,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    user_message_count INTEGER NOT NULL DEFAULT 0,
    assistant_message_count INTEGER NOT NULL DEFAULT 0,
    tool_use_count INTEGER NOT NULL DEFAULT 0,
    bytes_size INTEGER NOT NULL DEFAULT 0,
    file_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`metadata_json` is the escape hatch for source-specific fields
(conversation title, voice/text modality, originating account, model
version, etc.). It's never required for the overseer to function — the
top-level columns are enough — but it preserves provenance for anyone
doing forensic queries later.

---

## See also

- `plugins/overseer/POSTURE_NUMERIC_STREAMS.md` — how the overseer treats biometric / activity / phone data (source vs context distinction).
- `plugins/overseer/SECRETS.md` — secrets file format and rotation.
- `plugins/README.md` — plugin lifecycle and isolation model.
- `imports/` (NOT IN THIS REPO; lives at `~/cortex/imports/` in the larger workspace) — reference importer scripts for ChatGPT, grok.com, grok-twitter, tweets.
