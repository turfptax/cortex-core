# Deterministic loop — lights-out Cortex maintenance

`scripts/deterministic_loop.py` runs the **LLM-independent** subset of
the looper's work as a pure Python program. Use it when the Claude-
Code `/loop` AI can't run — OpenRouter credit exhausted, Anthropic
quota out, network down, you're on vacation, etc.

## Why this exists

Cycle 2 of the `/loop` (iters 21-27, 2026-06-07) proved that the
most valuable cycle-2 work — F1 abstraction-graph coverage push
(11.8% → 42.3%), decision mining (93 from 3,466 gists), entity
extraction — was ALL deterministic. The looper recognized this and
shipped it through the OpenRouter outage without burning credit.

This script captures that pattern as a runnable program. It's the
THIRD execution surface for Cortex maintenance:

| Surface | When it runs | Cost | Best for |
|---|---|---|---|
| Overseer tick loop on .25 | Background, every 15 min | OpenRouter $ | LLM interpretive work (gist summarization, journal, narratives) |
| Claude Code `/loop` | When you start it | Anthropic Max sub | Judgment calls, design work, novel datamining |
| **Deterministic loop (this)** | Cron / systemd / manual | **$0** | Maintenance that doesn't need an LLM |

## What it does

Each invocation:
1. Picks ONE work unit that's overdue
2. Runs it
3. Writes a `looper_log` row with `mode=deterministic` so the next
   Claude `/loop` cycle sees what landed

Work units currently shipped:

| Unit | Cadence | What |
|---|---:|---|
| `health_probe` | 1h | Pi ping + working_memory freshness + weather plugin alive. Cheap; runs first. |
| `vault_render` | 6h | Re-render vault + ghost sweep. Keeps the markdown corpus in sync with overseer.db. ~50s for 4,200 files. |
| `pull_event_stats_snapshot` | 12h | Snapshot F1 adoption signal (organic-external count) into looper_log so the trend persists across cycles. |
| `f1_coverage_snapshot` | 12h | Snapshot abstraction-graph coverage (the looper's cycle-2 mandate metric). |

## Usage

```bash
# Run once, picks the most-overdue unit. Most common pattern.
python scripts/deterministic_loop.py

# Cron-friendly: silent on no-op
python scripts/deterministic_loop.py --quiet

# Force-run a specific unit (skips overdue logic)
python scripts/deterministic_loop.py --unit vault_render

# List units + when each last ran
python scripts/deterministic_loop.py --list

# Different Pi
python scripts/deterministic_loop.py --pi http://other-pi:8420
```

## Cron / systemd installation

The cleanest deployment is a systemd timer on .25. Every 30 min,
let the picker decide whether anything is overdue.

```ini
# /etc/systemd/system/cortex-deterministic-loop.service
[Unit]
Description=Cortex deterministic maintenance loop
After=cortex-core.service

[Service]
Type=oneshot
WorkingDirectory=/home/turfptax/cortex-core
ExecStart=/usr/bin/python3 /home/turfptax/cortex-core/scripts/deterministic_loop.py --quiet
User=root
```

```ini
# /etc/systemd/system/cortex-deterministic-loop.timer
[Unit]
Description=Cortex deterministic loop every 30 min

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cortex-deterministic-loop.timer
sudo systemctl status cortex-deterministic-loop.timer
```

Alternatively, simple cron from any Windows / Mac / Linux host:

```cron
*/30 * * * * /usr/bin/python3 /path/to/deterministic_loop.py --quiet
```

## Adding new work units

Every unit is a callable returning a dict:

```python
def unit_my_new_thing(pi, auth, log_fn):
    """One-line description of what this does."""
    # ... your deterministic work, calls to /plugins/... endpoints ...
    return {
        "summary": "...",            # 1-paragraph TLDR
        "work_done": [{              # structured items
            "category": "cleanup",
            "item": "...",
            "status": "shipped",
        }],
        "followups": [],
        "escalations": [],
        "files_changed": [],
    }
```

Register it:

```python
UNITS = {
    ...
    "my_new_thing": (
        unit_my_new_thing, 24.0,
        "What it does.",
    ),
}
```

The runner picks it up automatically. Each unit MUST be idempotent
— if cron fires twice in 5 seconds, the unit should be a no-op or
safely repeatable the second time.

## What this is NOT

- Not a Claude-Code session. No LLM. No reasoning. Just SQL + HTTP.
- Not a replacement for the overseer's tick loop. The overseer
  still runs its own background work (journal, summaries, narratives).
- Not authority to do anything destructive. Same hard limits as
  the Claude-Code looper: no DELETE, no force-push, no schema
  rewrites. (Work units are vetted in advance; new ones are
  reviewed for safety before adding to the registry.)
- Not for novel datamining. If you can't define the work without
  asking an LLM what to do, it doesn't belong here.

## When credits return

The Claude `/loop` AI reads `looper_log` at boot. Deterministic-mode
rows (`mode=deterministic`) show up in the same stream with `model=
deterministic-script-no-llm` and `session_id=deterministic:<unit>`.
Cycle 3 will see them, understand what was maintained, and pick up
where the LLM-dependent work left off.

If the Claude looper finds deterministic maintenance was happening
while it was offline, that's good news, not noise. Don't dedupe it,
don't suppress it. The whole point is continuity across outages.

## Provenance

Designed 2026-06-08 in response to the OpenRouter credit exhaustion
that bridged cycles 2 and 3. The pattern was discovered by the
looper itself in iter 21 ("the loop pivoted to LLM-independent
datamining because Tory said 'find other work to data mine'"). This
script is a generalization of that lesson into a runnable form.
