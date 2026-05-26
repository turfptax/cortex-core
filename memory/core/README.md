# Core identity files

This directory holds three identity files that define the agentic triangle
inside Cortex: the **USER**, the **OVERSEER**, and the **APP** itself.

External AIs working with this Cortex instance should read all three (in
order: USER → OVERSEER → APP) before doing substantive work. They are
deliberately short and stable; the live corpus is where the dynamic
state lives.

## Files

| File | What it describes | Tracked in git? |
|---|---|---|
| `USER.md` | The human Cortex is built for | **No** — gitignored (personal) |
| `OVERSEER.md` | The memory-upkeep agent inside Cortex | **No** — gitignored (calibration personal) |
| `APP.md` | Cortex itself (architecture, functions, layers) | **No** — gitignored (deployment-specific) |
| `example.USER.md` | Public template for USER.md | **Yes** |
| `example.OVERSEER.md` | Public template for OVERSEER.md | **Yes** |
| `example.APP.md` | Public template for APP.md | **Yes** |
| `README.md` (this file) | Explains the pattern | **Yes** |

## Why hidden

The real `USER.md` contains personal information (real name, family,
finances, health, etc.). The real `OVERSEER.md` contains discipline notes
calibrated to a specific user's preferences. The real `APP.md` may name
deployment-specific paths and current state. None of these belong in a
public repo — but the pattern itself does.

The `.gitignore` uses negation to keep this safe:

```
memory/core/*.md
!memory/core/example.*.md
!memory/core/README.md
```

That ignores everything in this directory by default and re-allows only
the example files and this README.

## How AIs should use these files

When an external AI starts working with this Cortex instance:

1. Read `USER.md` to understand who Tory is, what he's working on, and
   what calibration he expects.
2. Read `OVERSEER.md` to understand the agent that maintains the memory
   layer and what discipline it operates under.
3. Read `APP.md` to understand Cortex's architecture, the three
   functions, the three layers, and the current state.

Then they can query Cortex's interpretive corpus (gists, themes,
narratives, journal) via MCP or via the vault.

## Regeneration

These files are mostly stable but the headers update. See each file's
own "How to update this file" section for the regeneration cadence.
