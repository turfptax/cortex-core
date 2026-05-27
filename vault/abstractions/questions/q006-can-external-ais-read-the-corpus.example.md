---
type: question
id: 6
slug: q006-can-external-ais-read-the-corpus
confidence: high
lifecycle: active
evidence_count: 5
first_filed: 2026-05-26
last_updated: 2026-05-27
sensitivity: public
source_hash: <TEMPLATE>
render_at: <TEMPLATE>
tags: [mcp, vault, corpus-access, foundational]
---

# Can external AIs actually read the Cortex corpus through MCP, or is the institutional memory layer sealed off from its declared audience?

## Generated below this line — edits above are preserved

### Question
<TEMPLATE: from open_questions.body — Filed 2026-05-26 by sibling
probe. notes_search returned 0 substantive hits on "Microsoft
tenant migration" despite 154 source conversations + 33 hits in
summaries_gist via direct LIKE. The interpretive layer (gists,
themes, episodes, journal, narratives) was invisible to external
AIs through the MCP surface.>

### Filed evidence
- [[g3340]] — supports (confidence: med) — 2026-05-26 — tenant migration result that surfaced the gap
- [[g3297]] — supports (confidence: med) — 2026-05-26
- [[g3277]] — supports (confidence: med) — 2026-05-26
- [[g3153]] — supports (confidence: med) — 2026-05-26
- [[g3141]] — supports (confidence: med) — 2026-05-26

### Status
Resolved by Phase 1 ship 2026-05-27. The new `cortex_search` MCP
tool surfaces hits across 11 interpretive tables. Lifecycle moves
to "resolved" once 7 days of pull_events confirm external AIs are
actually using the surface and the result-shape is calibrated.

Phase 2 (this vault) is the durable solution; Phase 1 is the
bridge.
