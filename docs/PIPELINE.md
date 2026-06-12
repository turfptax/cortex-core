# The Interpretation Pipeline: raw -> gist -> abstraction

Human-readable spec of how Cortex turns raw conversations into searchable
memory. Written 2026-06-10 alongside the pipeline-lab inspection tool
(`tools/pipeline_lab/`). If you change the logic, change this doc.

The design principle (locked 2026-05-02, Slice 3f.5): **each layer is lossy
on purpose, in a specific way.**

| Layer | What it keeps | What it drops | Query it answers |
|---|---|---|---|
| GIST | the CHANGE | everything already true before | "what changed this week?" |
| EPISODE | the SHAPE | the prose | "what happened?" |
| THEME | the RHYME | the specific contents | "what does this connect to?" |

## Stage 0: Raw input

Raw conversations arrive as:
- Claude Code `.jsonl` session files (one JSON object per line), parsed by
  `plugins/overseer/claude_jsonl.py::parse_claude_code_jsonl()`. Content
  blocks become plain text; tool calls become `[tool_use: NAME]` markers,
  tool results become 200-char `[tool_result: ...]` snippets, images become
  `[image]`.
- Rows in `overseer.db::imported_sessions` (ChatGPT/Grok/Twitter archives,
  git ingest, YouTube, etc.), each carrying source metadata + sensitivity.
- Live cortex.db sessions (notes grouped by session).

Before any LLM sees a conversation, `build_transcript_for_summary()`
formats it as one line per message:

```
[YYYY-MM-DD HH:MM U|A] <content, max 4000 chars per message>
```

Sidechain (branch-exploration) messages are skipped. If the transcript
exceeds 30,000 chars, it keeps the first 30 and last 20 messages with a
`[... K messages omitted ...]` marker between. This truncation is the
first lossy decision in the pipeline and it happens BEFORE the model.

## Stage 0.5 (PROPOSED, Lab-only as of 2026-06-11): session classification

Before any gist work, classify each session's NATURE so mechanical
high-volume threads stop skewing the memory system. Implemented in
`plugins/overseer/session_classifier.py`, exposed in Pipeline Lab, NOT
yet wired into loop.py (study-first directive).

| Category | Meaning | Weight | Treatment |
|---|---|---|---|
| human-dialogue | the user thinking / talking / creating | 1.0 | gist |
| human-build | user-directed work session, assistant executes | 0.8 | gist |
| automation-checkin | scheduled or recurring system session | 0.2 | rollup |
| automation-batch | programmatic traffic, negligible human input | 0.1 | rollup or skip |

Method: deterministic signals first (human-typed turns vs tool-result
echoes, tool-use fraction, scheduled-task markers in the opening turns,
message-length and repetition statistics), scored by transparent additive
rules where every point records the signal that produced it. One Flash
call (purpose `session-classify`) breaks ties only when the rule margin
is thin. Relationship to existing classifiers: orthogonal to
`category_classifier.py` (topic: work/cortex/personal) and a
session-granular refinement of Slice 3e's per-project `treat_as`
(human/automation/ignore), whose treatment vocabulary it reuses.

## Stage 1: Gist generation (one LLM call per conversation)

- Code: `loop.py::_summarize_one_imported()` (imports) and
  `_summarize_one_session()` (live sessions), loop steps 1 / 1c.
- Prompts: `prompts.py::import_gist_prompt()` / `session_gist_prompt()`.
  Core instruction: *"ONE LINE that captures THE CHANGE. What did this
  session change about the user's standing situation? Drop everything they
  already knew. Don't describe what the assistant did; describe what
  shifted for the human."* "No net change" is an explicitly valid answer.
- Sensitivity: sessions tagged `confidential`/`restricted` use
  `import_gist_prompt_sanitized()`: keep the structural shape of the work,
  be useless for reconstructing it.
- Model: purpose `summarize-session` -> `google/gemini-2.5-flash`
  (plugin.toml model_overrides; moved from Opus in Slice 14.7.1,
  ~$0.001/gist). max_tokens 160-200, temperature 0.4.
- Output: one sentence stored in `summaries_gist` (period_label like
  `claude-code:<id>`, confidence always `med` at birth, tags carry source
  + project). Every prompt appends MARKER_PRESERVATION_RULE so `[B:...]`
  / `[C:...]` agent-authorship markers survive summarization.
- Embedding: gists are embedded on write (bge-small via llama-embed on
  .25) into sqlite-vec for semantic recall.

## Stage 2: Evidence routing (one cheap call per new gist)

Immediately after a gist is born, `question_routing.py` asks Flash whether
it bears on any ACTIVE open question:

- Prompt lists active questions as Q1..Qn; the model answers zero or more
  lines of `Q<N>: supports|complicates|answers|reframes | <reason>`.
  Conservative by design; most gists route to nothing.
- Matches create `evidence_for_question` rows. Open questions are the
  continuity backbone: they accumulate evidence trails across months.

## Stage 3: Abstraction proposal (insight scan, per project arc)

Themes, patterns, and drift are NOT generated per conversation. They are
proposed by `insight_scan.py::scan_project_arcs()`, which reads a
project's recent GISTS (default 7-day window, formatted as
`g:<id> [confidence] body` lines, 8,000-char cap) plus the EXISTING
themes/patterns/drift lists, and asks for genuinely NEW insights only:

- THEME: a thread reinforced across multiple gists.
- PATTERN: recurring behavior or working style.
- DRIFT: something that started, stopped, or shifted.

Output contract: one JSON object,
`{"insights": [{kind, title, body, confidence, rationale,
supporting_gist_ids, direction?}]}`. The prompt forbids re-proposing
existing items, forbids low-confidence padding, and allows an empty list.
`parse_scan_response()` then enforces the same bar in code: drops non-JSON,
drops kinds outside theme/pattern/drift, drops anything self-rated `low`,
clamps field lengths, coerces gist ids to ints.

Model: purpose `insight-scan` -> `google/gemini-2.5-flash`, max_tokens
1500, temperature 0.3, cost cap $0.05/scan.

**Nothing is auto-applied.** Proposals land in `pending_interpretations`
and require confirmation (Hub Insights tab / chat) before
`apply_pending_interpretation()` promotes them into the live tables
(`summaries_theme`, `patterns`, `drift_observations`) and links theme
evidence via `theme_gists`.

Episodes share this proposal path (kind `episode`). Blindspots follow a
parallel path: `distill_corrections.py` clusters user corrections into
blindspot proposals (loop step 7). Open questions are created manually
(chat/API), never auto-proposed.

## Stage 4: Time-anchored synthesis (separate track)

Temporal narratives (daily/weekly/monthly/yearly) synthesize gists and
prior narratives on calendar triggers (loop step 0, 22:00 local). They are
prose, not extraction, and bypass the daily budget because missing a
period is permanent. Models: Sonnet for weekly+, per model_overrides.

## The dials (what you can tune, and where)

| Dial | Where | Current value |
|---|---|---|
| Transcript truncation | claude_jsonl.py build_transcript_for_summary | 30k chars, head 30 / tail 20, 4k/message |
| Gist framing | prompts.py import/session_gist_prompt | "THE CHANGE", one line |
| Gist model + params | plugin.toml [llm.model_overrides] summarize-session | gemini-2.5-flash, 200 tok, t=0.4 |
| Insight window | insight_scan.scan_project_arcs(days=) | 7 days, 200 gists max |
| Insight bar | SCAN_PROMPT_TEMPLATE + parse_scan_response | med+ confidence only, JSON contract |
| Routing conservatism | question_routing ROUTING_PROMPT_TEMPLATE | zero-or-more, reason required |
| Budgets | plugin.toml loop_* keys | $3/day, 1500 calls, $0.50/tick |

## Known weaknesses (as of 2026-06-10)

- Gists carry no span-level provenance: nothing records WHICH part of the
  raw transcript justified the sentence. The pipeline-lab tool adds
  post-hoc lexical alignment as instrumentation, but the pipeline itself
  does not store evidence spans.
- One gist per conversation regardless of length: a 6-hour multi-topic
  session and a 5-minute question get the same single sentence.
- The head/tail truncation can drop the middle of long sessions, which is
  exactly where mid-session pivots live.
- Theme coverage is semantically saturated at ~44% of gists (2026-06-11
  finding): more coverage needs NEW themes, not more links to old ones.
- Confidence is born `med` everywhere and rarely moves; it is not yet a
  meaningful ranking signal.
