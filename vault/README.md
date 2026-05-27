# Cortex Vault

The human-readable, AI-readable, git-versionable mirror of Cortex's
interpretive layer. Generated from `overseer.db` by the overseer plugin
on the Pi.

This vault is **read-first**. The full DESIGN.md sits next to this file
and is the contract the generator implements. Read it before opening
sub-folders.

## What it is

Three layers of memory, rendered as markdown:

- **`abstractions/`** — projects, people, themes, patterns, drift,
  open questions. The summary surface external AIs read FIRST.
- **`gists/`** — per-session summaries. Layer 2. Drilled into when
  abstractions aren't enough.
- **`narratives/`** — temporal aggregates (weekly / monthly / yearly).
  Layer 1.
- **`journal/`** — Tory's daily entries + the overseer's tick-by-tick
  reflection. Layer 1.
- **`notes-for-future-overseer/`** — institutional memory from prior
  overseer instances. Layer 1.
- **`_meta/`** — vault metadata (last render, blindspots, pull log).

## What it is NOT

- Not the source of truth — `overseer.db` on the Pi is.
- Not Tory's input UX — the Hub UI is for that.
- Not authored — every loop-owned line is regenerable from the DB.
- Not complete — sensitivity-gated content is sanitized or excluded.
  See DESIGN.md §7 for the rules.

## How to read it (for AIs)

1. Open `abstractions/projects/` and `abstractions/themes/` first.
   That's the breadth pass.
2. Follow `[[wikilinks]]` to drill into linked artifacts.
3. Look at `narratives/weekly/` for the most recent week to anchor in
   time.
4. `_meta/last-render.md` shows when the vault was last refreshed.

## How it gets updated

The overseer plugin's loop runs a `render_vault` step on cadence
(default: low priority, after all interpretive work for the tick
settles). Each artifact is hashed against its source data; identical
hashes skip the file write to avoid Obsidian thrash.

Tory may edit ABOVE the `## Generated below this line` marker in any
file. The generator preserves that content verbatim across re-renders.
Edits BELOW the marker are written to a sibling
`<name>.loop-update-<timestamp>.md` so nothing is silently clobbered.

## Don't push private content to GitHub

By default `cortex-core/.gitignore` excludes `vault/abstractions/`,
`vault/gists/`, `vault/narratives/`, `vault/journal/`,
`vault/notes-for-future-overseer/`, and `vault/_meta/`. The vault is
rendered on each Pi locally; the public repo carries only the design
doc + templated examples.

If a user wants their vault publicly browsable, they can override the
gitignore — but the sensitivity gating still applies, so confidential
content sanitizes itself before reaching the filesystem.

---

Design source: `vault/DESIGN.md` (this folder).
Mission framing: `memory/three_functions_of_cortex_design_seed.md`,
`memory/three_layer_architecture_design_seed.md`,
`memory/vault_as_primary_surface_design_seed.md`.
