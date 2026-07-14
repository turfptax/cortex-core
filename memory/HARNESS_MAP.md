# Cortex Harness Map

One succinct map of every screen and feature in the Cortex agent
harness. The overseer references this when a conversation is escalated
from any surface ("Discuss with Overseer"), so it knows what the user
was looking at.

RULE: update this file with EVERY user-facing feature, then redeploy
to the Pi. Keep entries to 1-2 lines. No personal data (tracked in the
public cortex-core repo).

Served live at GET /plugins/overseer/harness-map and via the
get_harness_map chat tool.

## Desktop Hub (cortex-desktop, localhost:8003)

- **Search** (#/search): search-first home over the whole corpus.
- **Corpus** (#/corpus/<tab>): the overseer's interpretive layers.
  - Overview: stats, working memory view, imported sessions, loop
    status, LLM cost.
  - Insights: pending interpretations queue (accept/reject).
  - Projects: per-project cards with narrative + stats; contains the
    collapsed Classification section (human/automation/ignore).
  - Squeeze: AI report card; model + task leaderboards from graded
    dispatches (Lemon Squeezer direction) + the conversations section
    (interaction feedback totals by surface/model + recent notes).
  - Ecosystem: map of AI actions (inspector).
  - Explorer: force-directed graph of questions/projects/patterns/
    themes/gists.
  - Bell: notifications from the overseer's rules engine, with custom
    action buttons; answerable from the phone too.
  - Contacts: overseer_people browser.
  - Voice: launcher for the Pipecat voice sidecar (:7860).
- **Chat** (#/chat): the agent-harness chat with the overseer.
  Threads sidebar (create/switch/rename/delete, auto-titled), prompt
  library picker in the composer, Flash-router default with Direct
  (Opus) override, file attachments, slash commands (/help /clear
  /compress /cost /tick /whoami /insights), tool-call audit under
  replies, per-turn feedback (rate + note; Discuss with Overseer
  opens a context-seeded thread).
- **Simples**: the planner mirror + permanent memory. Day (hour
  timeline of the phone's plan + a "This day in Cortex" panel: 24h
  ribbon of actual AI sessions and logged time, sessions with gists,
  health, journal, daily narrative, for ANY date), Week (7 mini
  timelines + teal sleep bars + weekend seam), Month (lived-activity
  heat from the corpus, plan as left-edge shape, sleep strips), Year
  (day-of-week aligned week columns with a detached Sat/Sun band,
  purple activity cells + teal sleep underlines, any year), plus a
  Weekly rhythm card (median + middle-half per weekday for activity/
  sleep/steps) and an "In the corpus" coverage barcode. Quiet layer
  rules: descriptive only, no scores or streaks; sleep teal #35a99b
  is the one second hue, length-encoded. Corpus data via Pi routes
  /plugins/overseer/day and /day/heat (s/sc/t/z/p/a).
- **Journal**: human journal (with voice transcription) + temporal
  narratives (D/W/M/Y) + overseer tick reflections.
- **System**: Activity (all AI runs, GitHub-style; rate dispatches
  here), Local LM (LM Studio chat), training pipeline, Pi status.
- **Settings**: config, updates (stable/dev), MCP setup.

## Phone app (cortex-mobile, Expo/Android)

- **Today**: daily brief, agenda, active projects, open question with
  answer-by-voice, "The overseer asks" Bell card (answer/dismiss +
  rate-this feedback row), Talk button into voice.
- **Simples**: liquid planner; goals -> auto-placed blocks around
  calendar anchors; Day/Week/Month/Year views; block tap sheet.
- **Projects**: project list + detail from the synced corpus.
- **Voice**: the primary capture surface. On-device STT/TTS +
  OpenRouter tool loop (notes, journal, time logging, calendar,
  people notes, project stubs, web, ask-overseer), model presets,
  per-chat cost, no-corpus toggle, chat history.
- **Memory**: browse the synced interpretive corpus (gists, journal,
  questions, people).
- **Settings**: local data stats, work-log reminders, sync (pending
  review + sync now), notification capture toggle, Pi config, Gateway
  cloud link. Sign in with Microsoft (OAuth 2.1 + PKCE, trusted-device
  hub scope, 30-day token, reconnect banner + re-auth on 401) or the
  advanced paste-a-token path; a badge shows pending AI-connection
  approvals.
- **AI Connections**: the owner's approval surface for external AIs
  (Grok, Claude, ChatGPT) connecting through the gateway. New ones
  land default-deny (pending); approve with a level (metadata / full),
  toggle ask-vs-always on reconnect, revoke (cuts tokens immediately).
- **Capture**: quick journal/note entry with type + project tag.
- **Pending upload**: pre-sync review; tap any item to omit it from
  the corpus.
- Sync: local-first SQLite; push/pull with the Pi on home WiFi
  (uuid-idempotent, piOnly kinds never leave the LAN).

## Pi core (cortex-core on .25, port 8420)

- **Overseer plugin**: background loop (imports -> gists -> themes ->
  narratives -> questions -> journal -> missions), chat handler
  (Flash router + Opus overseer, ~36 internal tools + external MCP
  connector tools), chat threads + prompt library, notifications
  rules engine, budget guard, vector index (sqlite-vec semantic
  search), sensitivity gating.
- **Feedback layer**: interaction_feedback table; any rated/noted
  interaction; "discuss" seeds a chat thread with injected context.
- **Health data**: health_daily table (day x metric daily rollups:
  steps, sleep, scores, resting HR). Backfilled from Fitbit Takeout
  via scripts/fitbit_takeout_ingest.py; feeds D20 STR/CON + Simples.
- **MCP connectors**: the Pi is the MCP client (Option B, locked
  2026-07-11). Registered HTTP MCP servers contribute tools to the
  overseer's own tool loop as mcp_<connector>_<tool>.
- **Tech skills + rules** (2026-07-12): living skills portfolio
  (tech_skills + tech_skill_log) and tech-decisions rules log
  (tech_rules). AI sessions log lessons via cortex_skill_log /
  cortex_rule_add MCP tools; every connecting AI gets the active
  rules digest in /intro; overseer chat reads them via
  get_tech_rules. Routes: /skills, /skills/get, /skills/log,
  /rules, /rules/add.
- **Sync plugin**: phone push/pull contract v2 + Gateway forward.
- **Weather plugin**: NWS polling + alerts into the Bell.
- **MCP server surface**: cortex_* tools for external AIs (search,
  people, projects, notes, sessions, overseer_chat).

## Other surfaces

- **Voice sidecar** (desktop :7860): Pipecat real-time voice with
  barge-in, Kokoro TTS, overseer as the LLM.
- **Gateway** (Azure): remote connector path with sensitivity gate +
  exfil monitor; phone pushes disabled (home-sync only).
- **GistLens** (dev tool): run the real gist pipeline on pasted
  conversations.
- **Pipeline lab** (dev tool, :8777): prod prompts against a scratch
  db.
