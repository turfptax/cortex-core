# Cloud migration (canonical direction) 2026-07-14

> **This is the active cross-repo direction.** cortex-core, cortex-desktop,
> cortex-mobile, and cortex-gateway all change. Read the "your team" section for
> your repo. Full plan (diagram, phases, cost): the "Solo cloud migration"
> artifact (ask Tory for the link). This supersedes the standalone-Pi model.

## The target, in one paragraph

Retire the home Pi. Move one person's Cortex fully into Azure as ONE Container
App: the **core** (single writer of cortex.db + overseer.db, runs the loop) and
the **gateway** (public OAuth 2.1 edge, reads the corpus in SQLite mode) share
one SQLite file-set on the app's local volume, backed up to Blob by a Litestream
sidecar, scale-to-zero when idle. Single-owner: each deployment is one person
(their Microsoft account is the owner), so there is NO multi-tenant control
plane, NO signup service, and NO cross-tenant isolation code. The whole thing is
a parameterized deploy you copy per friend at near-zero monthly cost (each person
brings their own OpenRouter key).

Why this is cheap and low-risk: the gateway already has a SQLite mode
(`CORTEX_DB_PATH` / `OVERSEER_DB_PATH`), and SQLite WAL allows many readers with
one writer, so the gateway reads the core's live files directly. That deletes the
separate Azure SQL mirror AND the desktop live-forward daemon that currently
bridge the Pi and the cloud.

## Dependency order (do not start client work before the cloud exists)

1. **P0 + P1 (cortex-core, no Azure needed):** env-externalize the core; prove
   the gateway reads the core's SQLite locally with no Azure SQL and no desktop
   bridge.
2. **P2 + P3 (core + gateway):** containerize, Litestream to Blob, deploy one
   Azure Container App, scale-to-zero, cron-driven tick.
3. **P4 (data):** migrate Tory's cortex.db + overseer.db + imports into the cloud.
4. **P5 (desktop + mobile):** flip the clients off the Pi; unplug the Pi.
5. **P6:** the friend-deploy script.

Desktop and mobile client changes are GATED on the cloud app existing (P3). Until
then, keep the current Pi behavior working.

## Team roles

| Repo | Becomes | Retires | Keeps |
|---|---|---|---|
| **cortex-core** (this repo) | the cloud engine (writer + loop), co-deployed with the gateway in one app | the Pi as a deployment target; the sync-plugin's push-to-gateway-mirror role | the whole overseer engine, schema, loop logic, sqlite-vec (a deploy/config change, NOT an engine rewrite) |
| **cortex-gateway** | one container in the same app; reads the core SQLite in SQLite mode; still the OAuth 2.1 server + connector grants | its separate Azure SQL backend for personal deployments | OAuth, connector grants, sensitivity gate, exfil monitor |
| **cortex-desktop** | GUI served from the cloud + a small LOCAL Claude-file ingester | the Pi-proxy Hub backend; the live-forward daemon | a lightweight local agent that ingests Claude Code / Claude Desktop .jsonl and pushes to the cloud (only it can see those files); optionally local whisper |
| **cortex-mobile** | a direct cloud client | the Pi LAN transport as the primary path (demote to opt-in) | local-first SQLite, sync, the already-shipped OAuth + AI Connections |

## cortex-core: your near-term work (P0 + P1, no Azure)

**P0 env-externalize (also fixes a real bug):**
- Make env-driven: `CORTEX_DB_PATH`, `OVERSEER_DB_PATH`, the plugin data dir, the
  Basic-auth pair (to a `CORTEX_SERVICE_TOKEN` env), the LAN model fallbacks (to a
  cloud-only chain), the OpenRouter key (env-only in cloud), and a
  `CORTEX_TENANT_TZ`.
- Fix the host-timezone budget-day bug in `loop.py` (the daily budget currently
  rolls on the host TZ, not the owner's TZ; the cloud container runs UTC).
- Add `CORTEX_LOOP_MODE=external` so the in-process 15-min daemon is off in cloud;
  the loop is driven by an external `POST /tick-now`.
- **Exit test:** copy real cortex.db + overseer.db to a temp dir, boot the core
  pointed there by env only, confirm it opens the vec index, runs one
  `/tick-now` producing a gist, and rolls the budget on the tenant TZ. Replay an
  OLD-shape DB copy (CREATE TABLE IF NOT EXISTS + ensureColumn drift) and confirm
  boot succeeds.

**P1 gateway-reads-core-SQLite (coordinate with the gateway team):**
- Run the gateway in SQLite mode pointing at the core's live DB files, co-located,
  with no Azure SQL and no desktop bridge.
- The sync plugin's job changes: it stops needing the desktop daemon to forward to
  a gateway mirror; the phone PULLS from the cloud gateway (which reads the same
  files). Confirm the read surface the gateway needs is fully served off SQLite
  (the vector search stays a core endpoint; the gateway may only need
  substring/relational reads without loading sqlite-vec).
- **Exit test:** locally, the gateway serves `/v1` search + AI Connections against
  the core's live SQLite, and a fresh gist written by a loop tick is instantly
  visible via `/v1` with no sync step.

## What does NOT change

The overseer loop logic, the interpretive pipeline, the schema, the sqlite-vec
index, the chat/tools surface. This migration is about WHERE the core runs and
HOW it is stored and woken, not what it does.
