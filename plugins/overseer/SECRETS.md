# Overseer Plugin — Secrets & Key Rotation

The overseer plugin's `LLMRouter` needs an OpenRouter API key for the
default cloud backend. This document describes where the key lives,
how the loader finds it, and how to rotate it safely.

## What the plugin needs

| Secret | Used by | Required? |
|---|---|---|
| OpenRouter API key | `openrouter` backend in `LLMRouter` | Required if you want cloud-quality LLM calls. The plugin still loads without it; it just falls back to `lmstudio` then `ondevice`. |

**Not stored as secrets** (these are config, not credentials):
- LM Studio URL (`10.0.0.102:1234` by default) — in `plugin.toml`
- On-device URL (`127.0.0.1:8081`) — in `plugin.toml`
- Pi Basic Auth (`cortex:cortex`) — set in core, not in this plugin

## Where secrets live

### On the Pi (where the plugin runs)

Canonical path: **`/home/turfptax/.cortex/secrets.toml`**, mode `0600`.

```toml
# ~/.cortex/secrets.toml
[openrouter]
api_key = "sk-or-v1-..."
```

The cortex-core systemd service runs as `root`, so a bare `~` resolves
to `/root` for the running process. The router resolves this via the
`secrets_paths` candidate list in `plugin.toml` (see below).

### On the Hub (Windows desktop)

Canonical path: **`%APPDATA%/Cortex/config.json`**.

```json
{
  "openrouter_api_key": "sk-or-v1-...",
  "openrouter_url": "https://openrouter.ai/api/v1",
  "openrouter_default_model": "anthropic/claude-opus-4.7"
}
```

The Hub uses this when *Hub-side* code makes OpenRouter calls. The
overseer plugin itself only reads the Pi-side secrets file; the Hub
copy exists so future Hub services (chat, training, etc.) can use the
same key without a second source of truth.

## How the loader finds the key

`llm_router.py::_load_secrets()` walks a candidate list, returning the
first file it can read. Order:

1. **`CORTEX_SECRETS` env var** — if set, used as the only path. Useful
   for tests or non-standard deployments.
2. **`[llm].secrets_paths`** in `plugin.toml`. Default list:
   ```toml
   secrets_paths = [
     "/home/turfptax/.cortex/secrets.toml",
     "/root/.cortex/secrets.toml",
     "/etc/cortex/secrets.toml",
     "~/.cortex/secrets.toml",
   ]
   ```
3. **Fallback:** `/etc/cortex/secrets.toml`, then `~/.cortex/secrets.toml`.

Within the loaded TOML, the key is read as `[openrouter].api_key`. The
env var **`OPENROUTER_API_KEY`** overrides the file value if set.

On startup, the plugin logs which file was used:
```
plugin.overseer.llm INFO  openrouter key loaded from /home/turfptax/.cortex/secrets.toml (73 chars)
```
or, if no key was found:
```
plugin.overseer.llm INFO  no openrouter key found in any of N candidate paths;
                          openrouter backend will fail
```

## Rotation procedure

When you need to rotate (key compromise, periodic rotation, leaving a
project, etc.):

### Step 1 — create the new key on OpenRouter

1. Go to <https://openrouter.ai/settings/keys>.
2. Click **Create Key**. Name it something dated (`cortex-pi-2026-05`).
   Set a credit limit appropriate for the deployment.
3. Copy the new `sk-or-v1-...` value. **You will not see it again.**

### Step 2 — install the new key

The fastest way is to ask Claude (or whoever's driving) to do both at once:

> *"Update the OpenRouter key in both places. New key:* `sk-or-v1-NEW...`*"*

Manual installation:

**Pi:**
```bash
ssh turfptax@10.0.0.25
cat > ~/.cortex/secrets.toml << 'EOF'
[openrouter]
api_key = "sk-or-v1-NEW..."
EOF
chmod 600 ~/.cortex/secrets.toml
sudo systemctl restart cortex-core
```

**Hub (Windows):**
Edit `%APPDATA%/Cortex/config.json`, replace the `openrouter_api_key`
value. No service restart needed for the Hub side (config is reloaded
on next call).

### Step 3 — verify the new key works

```powershell
curl.exe -u cortex:cortex -X POST -H "Content-Type: application/json" `
  -d "{\"prompt\":\"hi\",\"max_tokens\":20,\"purpose\":\"post-rotation-test\"}" `
  http://10.0.0.25:8420/plugins/overseer/llm/test
```

Expected: `ok: true`, `backend: openrouter`, `degraded: false`. If you
see `degraded: true` and `backend: lmstudio` or `ondevice`, the new key
isn't being read — check the systemd logs:
```
ssh turfptax@10.0.0.25 "sudo journalctl -u cortex-core -n 100 --no-pager | grep openrouter"
```

### Step 4 — revoke the old key on OpenRouter

Only do this **after** Step 3 confirms the new key works. Once revoked,
the old key is dead immediately — any in-flight call using it will
fail.

1. Back in <https://openrouter.ai/settings/keys>, find the old key.
2. Click **Revoke** (or **Delete** depending on UI).
3. Done.

## When to rotate

- **Immediately** after any of: key pasted into chat/email/Slack/git;
  laptop or Pi compromised; team member leaves; OpenRouter sends a
  security alert.
- **Periodically** — every 90 days is a reasonable cadence for a
  personal-use deployment. Set a calendar reminder.
- **Never reuse** an old key after revocation — always create a new
  one.

## What if the key leaks?

OpenRouter has built-in defenses you should rely on:
- **Per-key credit limits** — set a low cap when creating the key so
  even a leak can't burn the whole account. The dashboard at
  <https://openrouter.ai/settings/keys> shows usage per key.
- **Per-key allowed-models list** — restrict the key to the models you
  actually use (Opus 4.7 + Sonnet, say). Prevents an attacker from
  running cheap-to-call abuse on expensive models.
- **Activity log** — OpenRouter logs every call with timestamp + IP +
  model + cost. Audit any time at
  <https://openrouter.ai/activity>.

If a leak happens: revoke first (Step 4), then rotate (Steps 1-3).
Order matters — revoking before rotating means a brief outage but no
window for misuse.

## Local file hygiene

- The Pi secrets file is `chmod 600` — owner read/write only. Verify
  with `ls -la ~/.cortex/secrets.toml` (should show `-rw-------`).
- The Hub config is in `%APPDATA%`, which is per-user on Windows. Don't
  copy it to a shared drive.
- `~/.cortex/` should be in your global gitignore at the user-home
  level — e.g. add `.cortex/` to `~/.config/git/ignore` so it can never
  be accidentally committed even if you `git init` inside `~/`.
