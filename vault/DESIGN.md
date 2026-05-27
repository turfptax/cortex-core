# Cortex Vault — Design

**Status: locked 2026-05-27, sibling Task #19 deliverable.**
**Reading time: ~12 minutes.**

## TL;DR

The vault is markdown + YAML frontmatter mirroring the three-layer
interpretive corpus. Frontmatter is loop-owned; body has a marker
above which Tory's hand-edits survive every re-render. Files cross-
reference via `[[slug]]` wikilinks that resolve in Obsidian and degrade
gracefully in plain markdown viewers. Generation is idempotent + per-
file hashed to avoid Obsidian thrash. Confidential content sanitizes
at render time; restricted is excluded entirely. Raw never enters the
vault.

This design extends two locked seeds and does not contradict them:

- `memory/three_layer_architecture_design_seed.md` — defines the
  three layers and the pull-driven refinement loop.
- `memory/vault_as_primary_surface_design_seed.md` — locks the vault
  as primary reader surface, sets sensitivity gating posture.

## 1. Directory structure

```
vault/
├── README.md                 (public, ships in repo)
├── DESIGN.md                 (public, this file)
├── _meta/
│   ├── last-render.md        timestamp + per-folder counts
│   ├── blindspots.md         known_blindspots table → one section per row
│   ├── pulls.md              recent pull_events Tory can audit
│   └── vault-stats.md        node/edge counts for graph view sanity
├── abstractions/                       ← Layer 1 (read FIRST)
│   ├── projects/
│   │   ├── openmuscle-web.md
│   │   ├── ClientA-compliance.md
│   │   └── sidegig-exit.md
│   ├── people/
│   │   └── jane-mentor.md
│   ├── themes/
│   │   └── continuity-of-self.md
│   ├── patterns/
│   │   └── pattern-196-frame-survival.md
│   ├── drift/
│   │   └── drift-12-milestone-anniversary-pull.md
│   └── questions/
│       └── q006-can-external-ais-read-the-corpus.md
├── gists/                              ← Layer 2 (drilled)
│   └── 2026/
│       └── 05/
│           └── 26/
│               └── g3141-bittitan-migrationwiz-checklist.md
├── narratives/                         ← Layer 1 (temporal)
│   ├── weekly/
│   │   └── 2026-W21.md
│   ├── monthly/
│   │   └── 2026-05.md
│   └── yearly/
│       └── 2026.md
├── journal/
│   ├── daily/                          (human_journal_entries)
│   │   └── 2026-05-26.md
│   └── overseer/                       (overseer_journal table)
│       └── tick-00375.md
└── notes-for-future-overseer/          (future_overseer_notes table)
    └── 2026-05-27-corpus-discovery-reframe.md
```

### Deviations from the seed prompt's "at minimum" list

- **`drift/`** added at the abstraction layer. The seed prompt listed
  it under generic abstractions but didn't break it out; drift is a
  distinct artifact type (it's about state-change, not state) and gets
  its own folder.
- **`_meta/`** carries `pulls.md` per the new `pull_events` table
  (Phase 1 ship). External AIs and Tory can audit what's been drilled.
- **Gists are date-bucketed (`gists/YYYY/MM/DD/`)** to keep any one
  folder under ~100 files. 3,450 gists in one flat folder would brown
  out Obsidian.
- **No top-level `episodes/`.** Episodes are rendered inline under
  `narratives/weekly/` as section headers; the standalone-file model
  was overhead for low-volume artifacts. (Open: revisit if episode
  count grows >50.)

## 2. File templates

See `vault/abstractions/projects/_template.md` etc. for the canonical
shape; abbreviated below.

### Project (`vault/abstractions/projects/<tag>.md`)

```markdown
---
type: project
tag: openmuscle-web
status: active
category: openmuscle
github_url: https://github.com/turfptax/openmuscle-web
total_minutes_active: 4287
last_touched: 2026-05-25
session_count: 142
sensitivity: internal
source_hash: <sha256 of source rows>
render_at: 2026-05-27T04:30:00-05:00
---

# openmuscle-web

<!-- Tory's free-text notes go here. Preserved across re-renders. -->

## Generated below this line — edits above are preserved

### Narrative
{project_summaries.narrative_text}

### Recent sessions
- 2026-05-25 — [[g3140]] firmware EMG threshold tuning
- 2026-05-24 — [[g3138]] PCB v3 spine-vs-cover-story diagnosis
- ...

### Linked themes
- [[continuity-of-self]]
- [[institutional-cluster]]

### Open questions
- [[q006-can-external-ais-read-the-corpus]] (confidence: high)

### Linked people
- [[tory-hayes]] — collaborator on EMG sensor selection
```

### Person (`vault/abstractions/people/<slug>.md`)

```markdown
---
type: person
name: Jane Mentor
slug: jane-mentor
role: father / consulting partner
org: Jane Mentor Leadership Consulting
expertise_tags: [leadership, consulting, political-strategy]
first_mentioned: 2025-01-12
mention_count: 47
sensitivity: internal
source_hash: <sha256>
render_at: 2026-05-27T04:30:00-05:00
---

# Jane Mentor

## Generated below this line — edits above are preserved

### Relationship context
{overseer_people.body}

### Linked projects
- [[jane-mentor-leadership-consulting]] — strategic engine role
- ...

### Recent activity references (last 3)
- 2026-05-22 — [[g3120]] white-paper review
- ...
```

### Theme (`vault/abstractions/themes/<slug>.md`)

```markdown
---
type: theme
id: 4
slug: continuity-of-self
confidence: high
created_at: 2026-05-12
last_observed_at: 2026-05-25
evidence_count: 23
sensitivity: internal
source_hash: <sha256>
render_at: 2026-05-27T04:30:00-05:00
---

# Continuity of self

## Generated below this line

### Claim
{summaries_theme.body}

### Evidence
- [[g3140]] — supports — 2026-05-25
- [[g3098]] — complicates — 2026-05-18
- [[g2941]] — reframes — 2026-05-02
- ...

### Blindspot context
- [[blindspots#opus-overhedges-identity]] applies — confidence
  upgraded from med to high per the +1 adjustment rule.
```

### Open Question (`vault/abstractions/questions/q<NNN>-<slug>.md`)

```markdown
---
type: question
id: 6
slug: q006-can-external-ais-read-the-corpus
confidence: high
lifecycle: active                  # active | drowning | resolved | retired
evidence_count: 5
first_filed: 2026-05-26
last_updated: 2026-05-27
sensitivity: internal
source_hash: <sha256>
render_at: 2026-05-27T04:30:00-05:00
---

# Can external AIs actually read the Cortex corpus through MCP?

## Generated below this line

### Question
{open_questions.body}

### Filed evidence
- [[g3340]] — supports (confidence: med) — tenant migration search
  surfaced the gap
- [[g3297]] — supports (confidence: med)
- ...

### Status
Resolved by Phase 1 (2026-05-27) — substring `cortex_search` MCP
tool now surfaces hits across 11 interpretive tables. Lifecycle
will move to "resolved" once 7 days of pull_events confirm external
AIs are actually using it.
```

### Daily Journal (`vault/journal/daily/YYYY-MM-DD.md`)

Tory's `human_journal_entries` rows for the date, ordered by time.

```markdown
---
type: journal-daily
date: 2026-05-26
entry_count: 3
source_hash: <sha256>
render_at: 2026-05-27T04:30:00-05:00
---

# 2026-05-26

## Generated below this line

### 08:42 — voice
{human_journal_entries[0].text}

### 14:11 — free
{human_journal_entries[1].text}

### 22:35 — free
{human_journal_entries[2].text}
```

### Overseer Journal (`vault/journal/overseer/tick-NNNNN.md`)

```markdown
---
type: journal-overseer
tick_id: 375
tick_at: 2026-05-26T10:59:00-05:00
provenance: anthropic/claude-sonnet-4.6
referenced_artifacts: [g3340, q006, p17]
sensitivity: internal
source_hash: <sha256>
render_at: 2026-05-27T04:30:00-05:00
---

# Tick #375 — corpus reframe locked

## Generated below this line

{overseer_journal.entry}

### Drilled into
- [[g3340]] — tenant migration result that surfaced the search gap
- [[q006-can-external-ais-read-the-corpus]] — the open question this
  tick was about
```

### Notes for Future Overseer (`vault/notes-for-future-overseer/YYYY-MM-DD-<slug>.md`)

```markdown
---
type: future-note
id: 1
author_instance: opus-4.7@2026-05-27
written_at: 2026-05-27T04:30:00-05:00
sensitivity: internal
source_hash: <sha256>
---

# Corpus discovery reframe — what next overseer should know

## Generated below this line

{future_overseer_notes.body}
```

### Pattern / Drift / Episode

Same shape as Theme — minor frontmatter differences (`direction`,
`occurrences`, `span_start`, etc.). Templates committed under
`vault/abstractions/patterns/_template.md` etc.

## 3. Wikilink conventions

**Slug rules** (deterministic, must round-trip):

| Source                      | Slug                                              |
|-----------------------------|---------------------------------------------------|
| Project tag                 | use the tag as-is (`openmuscle-web`, `ClientA-compliance`) |
| Person name                 | lowercase, ASCII-fold, `-` for spaces (`Jane Mentor` → `jane-mentor`) |
| Theme / pattern / drift     | id-prefix + lower-kebab title (`pattern-196-frame-survival`) |
| Open question               | `q<NNN-zero-padded>-<lower-kebab-truncated-30>`   |
| Gist                        | `g<id>` (no descriptive suffix in the link target — the file's filename carries the suffix, but the link uses `[[g3141]]` to stay readable inline) |
| Temporal narrative          | ISO label (`2026-W21`, `2026-05`, `2026`)         |
| Overseer journal            | `tick-<id-zero-padded-5>`                         |
| Future note                 | filename as-is                                    |
| Daily journal               | ISO date (`2026-05-26`)                           |

**Aliases:** Obsidian supports frontmatter `aliases: [...]`. Use for
people who go by multiple names (`name: "Jane Mentor"`, `aliases:
["Ali", "Dad"]`) and projects with prior names. NOT for gists/themes
— too many to maintain.

**Wikilink form:** prefer the short form `[[g3141]]` when the target
is unambiguous across the vault. Use `[[folder/slug|display text]]`
when disambiguating between a person and a project of the same name.

**Graceful degradation:** every wikilink target also corresponds to a
real filename (the vault's filename = the slug + `.md`). A plain
markdown viewer sees the bracketed text; an Obsidian viewer sees a
clickable link. No proprietary extensions required.

**Tags vs. wikilinks:** wikilinks for relationships between
artifacts. Tags (`#high-confidence`, `#category-work`,
`#sensitivity-confidential`) for filter dimensions Tory wants to
slice the graph by in Obsidian. Tags live in frontmatter
`tags: [...]`, not inline.

## 4. Regeneration strategy

**Cadence:** loop step `step_NN_render_vault` runs once per tick at
LOW priority, AFTER all interpretive steps for the tick. Plus a chat
tool `render_vault_now()` for on-demand regen, plus the existing
HTTP route `POST /plugins/overseer/vault/render` (Phase 2 ships).

**Per-file hash skip:** every file's frontmatter carries
`source_hash: <sha256>`. The hash inputs are the table rows + sort
order + sensitivity rules that produced the body. On render, the
generator computes the candidate hash and compares; if equal, no
write. This avoids Obsidian's file watcher firing on every render.

**Atomic swap:** the generator writes into `vault.tmp/` then renames
to `vault/` (with a backup of the previous `vault/` at
`vault.prev/`). Failure mid-render leaves `vault/` intact. The next
render's atomic swap drops `vault.prev/` from the previous run.

**Hand-edit boundary:** every loop-owned file has a literal marker:

```
## Generated below this line — edits above are preserved
```

The generator:
1. Reads the existing file (if present).
2. Splits at the marker.
3. Re-writes frontmatter (always loop-owned).
4. Re-emits Tory's `<title>` line + everything between title and the
   marker (preserved verbatim).
5. Re-writes the marker.
6. Re-emits the loop-owned body below the marker.

**Below-marker edits:** if the existing file's below-marker content
hash doesn't match what the loop wrote last time, Tory edited the
loop-owned section. Write the new loop body to
`<name>.loop-update-<timestamp>.md` next to the file; the original
stays intact. Tory merges by hand. Never silently clobber.

## 5. Conflict resolution

The hand-edit boundary above IS the conflict resolution mechanism.
Three rules:

| Scenario | Resolution |
|---|---|
| Frontmatter conflict (Tory edited frontmatter) | Loop wins, frontmatter is rewritten every render. Frontmatter is mechanically-derived; Tory's intended-edit-place is the body. |
| Above-marker body conflict | Tory wins, content preserved verbatim. |
| Below-marker body conflict (Tory edited loop output) | Write loop's new body to a sibling `*.loop-update-<ts>.md`. Tory merges manually. |

This is *append-only-safe* for the loop, *edit-anywhere-safe* for
Tory above the marker, and *visibly-merged-rather-than-clobbered* for
the edge case. No three-way merge required.

## 6. MCP tool surface

Already locked by `memory/mcp_surface_redesign_seed.md`. Five tools:

| Tool | Phase | What |
|---|---|---|
| `cortex_search(q, kinds, ...)` | **Phase 1 (shipped 2026-05-27)** | Substring across interpretive tables. Phase 3 swings to vault `.md` files. |
| `cortex_overseer_detail(token)` | **shipped already** | Resolves working-memory token → full row + next_tokens. |
| `cortex_read(path)` | Phase 3 | Read one vault file's content. |
| `cortex_list(folder, glob)` | Phase 3 | Directory listing within the vault. |
| `cortex_graph(slug)` | Phase 3 | Wikilink graph traversal — returns linked artifacts. |
| `cortex_recent(days)` | Phase 3 | What changed in the vault in the last N days. |

Pull events fire on each tool call (`mcp:cortex_search`,
`mcp:cortex_read`, etc.). Existing `cortex_overseer_detail` already
records pull_events as of Phase 1.

The 40+ legacy tools (`pet_*`, `wifi_*`, `shell_exec`, generic
`upsert_row` / `delete_row`, all `audit_*`) get deprecated when
Phase 3 ships. Sibling-dispatch + ingest tools move to an
`admin/` namespace.

## 7. What gets excluded

Per `vault_as_primary_surface_design_seed.md` and Slice 13 sensitivity
tiers:

| Tier | Render? | How |
|---|---|---|
| `public` | Yes | Full body, full frontmatter. |
| `internal` (default) | Yes | Full body, full frontmatter. |
| `confidential` | Yes, sanitized | Title preserved. Body rendered using the Slice 13 sanitized-gist prompt's output. Frontmatter carries `sensitivity: confidential`. |
| `restricted` | No | Excluded entirely from the vault filesystem. Available only via authenticated MCP `cortex_overseer_detail` with explicit user override. |
| Raw `imported_sessions` content | **No** | Never lands in the vault. A gist that came from a raw session carries `raw_id` in frontmatter; the MCP `cortex_read(raw_id)` is the only way to fetch raw, and it re-applies sensitivity rules at fetch time. |

**Vault is safe to push to a public GitHub repo only if the
configured `vault_render_sensitivity_ceiling` is set to `confidential`
or below AND no `restricted` content leaks via misclassification.**
Per cortex-core's current `.gitignore`, the rendered vault content
folders are excluded by default. Tory opts into git-tracking by
removing the exclusion.

## 8. Initial skeleton

This commit ships:

```
vault/
├── README.md
├── DESIGN.md
├── _meta/
│   ├── _template-last-render.md
│   ├── _template-blindspots.md
│   ├── _template-pulls.md
│   └── _template-vault-stats.md
├── abstractions/
│   ├── projects/
│   │   ├── _template.md
│   │   ├── openmuscle-web.example.md
│   │   ├── ClientA-compliance.example.md
│   │   └── sidegig-exit.example.md
│   ├── people/
│   │   ├── _template.md
│   │   └── jane-mentor.example.md
│   ├── themes/
│   │   ├── _template.md
│   │   └── continuity-of-self.example.md
│   ├── patterns/
│   │   ├── _template.md
│   │   └── pattern-196-frame-survival.example.md
│   ├── drift/
│   │   └── _template.md
│   └── questions/
│       ├── _template.md
│       └── q006-can-external-ais-read-the-corpus.example.md
├── gists/
│   └── _template.md
├── journal/
│   ├── daily/
│   │   ├── _template.md
│   │   └── 2026-05-26.example.md
│   └── overseer/
│       ├── _template.md
│       └── tick-00375.example.md
├── narratives/
│   ├── weekly/
│   │   └── _template.md
│   ├── monthly/
│   │   └── _template.md
│   └── yearly/
│       └── _template.md
└── notes-for-future-overseer/
    ├── _template.md
    └── 2026-05-27-corpus-discovery-reframe.example.md
```

Example files use `<TEMPLATE>` placeholders for any personal data
fields so the skeleton is safe to commit publicly. The generator
fills these from the DB at render time.

## Open decisions flagged for Tory

1. **Episode folder.** Punted — rendered inline under weekly
   narratives. Revisit if episode count grows above ~50.
2. **Daily journal entries from the wearable voice mode.** Currently
   stored alongside text entries in `human_journal_entries`. The
   vault renders both under `journal/daily/YYYY-MM-DD.md` ordered by
   time. If Tory wants voice-specific UI surfacing, that's a Hub
   concern, not vault.
3. **`vault_render_sensitivity_ceiling` default.** Proposed:
   `confidential`. That includes confidential-sanitized but excludes
   restricted. Override per-Pi via plugin.toml.
4. **gists folder pruning.** Currently emits ALL gists. 3,450 today;
   manageable. If the corpus grows past ~20k, consider rolling
   older gists into the monthly narrative's source-of-truth and
   tombstoning the file with a stub.
5. **The `notes-for-future-overseer/` folder is symbol-rich**
   (each note is dense, low-volume institutional memory). Could be
   surfaced as `_meta/future-overseer/` instead — closer to the
   "meta about this vault" content type. Going with the prompt's
   layout for now; happy to swap.
6. **MCP tool naming.** The seed locked `cortex_*` for the vault
   tools (Function 1). The currently-shipped `cortex_search` is
   already named correctly; the Phase 3 vault-aware versions can
   replace its substring-fallback transparently.
7. **Pull-event display granularity.** Phase 1 records each pull
   under a single `mcp:cortex_search` surface. Future: split into
   `mcp:cortex_search`, `vault:human-browse`, `hub:explorer`,
   `chat:overseer-reference`. Right now everything except detail-
   drill is `mcp:cortex_search`. Acceptable for v1; revisit when
   the vault generator ships and Tory's browse signals appear.

## Acceptance criteria for the generator (Phase 2 next slice)

1. `python -m plugins.overseer.vault_generator --out vault/` produces
   a tree that matches the skeleton above with real data.
2. Re-running with no DB changes is a no-op (zero file writes,
   verified by `last-render.md` count of `skipped: N, written: 0`).
3. The atomic swap leaves no half-rendered state on failure.
4. Below-marker hand-edits produce `*.loop-update-*.md` siblings,
   never overwrite.
5. `vault_render_sensitivity_ceiling=confidential` excludes any
   row tagged `restricted` from the filesystem entirely.
6. `_meta/last-render.md` carries timestamp, per-folder counts,
   per-table source-hash, sensitivity-ceiling-applied, and any
   skipped-due-to-hand-edit list.
7. Full render of the current corpus (3,450 gists + 374 journal
   entries + 214 narratives + ~90 patterns/drift/blindspots + 96
   open questions + people + projects) completes in <60s on the Pi.

## Summary

**Shipped in this design commit:**
- DESIGN.md (this file) + README.md
- 18 directories matching the locked structure
- 8 entity templates + 7 example files
- Conflict resolution mechanism (frontmatter loop-owned,
  body human-editable above marker, sibling-file on below-marker
  conflicts)
- Slug rules
- Sensitivity gating posture

**Punted on / decisions Tory should weigh in on:**
- Episodes folder (currently inlined under weekly narratives)
- gists folder pruning policy at scale (>20k gists)
- `vault_render_sensitivity_ceiling` default value
- Whether to relocate `notes-for-future-overseer/` under `_meta/`

**Next slice (Phase 2 generator):**
- `vault_generator.py` module under `plugins/overseer/`
- Loop step `_run_vault_render` registered after interpretive steps
- HTTP route `POST /plugins/overseer/vault/render`
- Per-template renderer (one function per entity type)
- Hash-based skip logic
- Atomic swap mechanics
- Pull-event integration so vault file reads (via cortex_read once
  Phase 3 ships) record pulls
