# Posture for numeric / behavioral streams

Status: Locked 2026-05-07 by sibling-Claude conversation with the
running overseer, post Slice 9. The overseer asked for this before
the next working-memory rebuild consumes any of the new data.

Context: insight #47 ("corpus expansion outruns posture decisions")
and #48 ("numeric streams require different evidence posture than
text") flagged that the overseer's existing extraction logic was
shaped by linguistic content (chatgpt-archive, claude-code sessions,
journal entries). The Slice 9 import doubled the corpus but with a
fundamentally different shape — 117k activity events, 152 day-rows
of body metrics, 821 calls, 228 voicemails. These don't speak; they
accumulate.

Without an explicit posture, the synthesis layer will either:
- under-weight numeric streams (treat them as atmosphere, not evidence),
  *or*
- over-weight them (find patterns in noise, especially around
  thresholds the device's ML already chose for marketing reasons).

This document fixes the posture before either failure mode
hardens into the working-memory builder's heuristics.

---

## 1. What each axis is *for* in synthesis

The fundamental split: each new surface is either a **source of new
claims** (a thing the overseer can author observations from), a
**context for existing claims** (data that enriches journal/session-
based observations but doesn't generate them), or both.

| Surface | Posture | Why |
|---|---|---|
| `body_metrics` (HRV, sleep score, SpO2, etc.) | **Context only.** Never a source for new claims about the user's interior state. | Numbers about a body don't tell you what the body's owner believed or decided. A 34 RMSSD morning enriches "I shipped Slice 7 today" — it does not author "the user was tired." Sleep-state language is downstream of the user's framing of it. |
| `body_response_events` (Fitbit-detected stress moments) | **Context only.** | Same reasoning. The device noticed something autonomic; that's data about the body, not about meaning. |
| `phone_calls` (counts + counterparties) | **Mixed.** Source for *relational* claims; context for *project* claims. | "User talked to {partner-A} weekly for 3 months" is a defensible source-claim. "User was working on {project-X} because they had 12 calls with {partner-B} that month" is context, not source — the work happened in sessions and notes, the calls just colocate it. |
| `voicemails` (substantive, length ≥ 50c) | **Source.** | Voicemails contain actual content (sender's words). Treat like an email or note from a third party. |
| `voicemails` (silent / hallucinated "you") | **Ignore.** | Already filtered via `surfaces_to_working_memory=0`. Don't surface them. |
| `activity_events.novelty_score` | **Source for behavioral claims, gated.** | A high-novelty day is evidence of a new thread; raw top-N isn't. |
| `activity_events` raw (single-row top-N) | **Context only.** | github.com / claude.ai / youtube every day is signal that a stable behavior persists, not that anything changed. |
| `phone_contacts.is_provisional` (13 entries) | **Hypotheses, not facts.** | Promoted in context only — when the user mentions a name in a journal/chat that matches a provisional row. Never auto-promoted. |

**Default rule:** if a claim could be authored from text alone (a
journal entry, a session gist), the numeric streams are context. If
a claim *requires* the numeric stream (e.g. "the user's call frequency
with {partner-B} spiked in March"), then the stream is source — but with
the minimum-evidence rules below.

## 2. Minimum-evidence rules for numeric streams

Patterns and themes have always required multiple-evidence support.
Numeric streams need stricter rules because their values are
continuous, not discrete utterances. A single anomalous day is rarely
meaningful; a sustained departure usually is.

### Body data
- **Single-day anomaly is never a theme.** A 22 RMSSD morning is
  noise unless it persists.
- **Pattern claims (sleep, HRV, body responses)** require ≥5 days
  within a 14-day window showing the same direction *and* a magnitude
  ≥ 20% off the rolling-30-day mean for that metric.
- **Sleep score:** < 60 for a single night = note-worthy datapoint
  (record it as context if a journal entry exists that day).
  ≥ 5 nights below 70 within 14 days = pattern candidate worth
  filing as `pending_interpretation`.
- **Body responses:** count > 1.5× the rolling median for ≥ 3
  consecutive days = elevated-stress pattern candidate.
- **Never claim cause from body data.** "Low HRV" is observable;
  "Low HRV *because* of stress" is not — that's a synthesis layered
  on top, and only authorable if the user's own words supply it.

### Activity / novelty
- **`novelty_score` ≥ 0.5 with `event_count` ≥ 10** = "new thread
  day" candidate. Single days are notable, not theme-worthy.
- **Theme claim** requires ≥ 3 days within a 7-day window showing
  novel domains/queries clustering on the same topic. The cluster
  detection is the synthesis work — don't claim a theme from raw
  event counts alone.
- **Top-N alone (without novelty) is never evidence of anything
  new.** It's evidence the stable behaviors persist. That's a
  context observation, not a pattern.

### Call patterns
- **"Frequent contact" claim** requires ≥ 4 weeks of consistent
  cadence (at least one call per 7-day window, or ≥ 4 calls/month).
- **"Increased contact" claim** requires a comparison window ≥ 30
  days before and ≥ 30 days after the alleged change point. Spikes
  inside a single month are noise unless persistent.
- **Counterparty inference:** never assert relationship type from
  call patterns alone. "Daily contact" + "long calls" + "asymmetric
  in-vs-out" suggests intimacy but doesn't prove it. Surface as
  observation, not characterization.

## 3. Journal-adjacency join

The body-context-on-journal join is **a read-only VIEW**, not a
materialized table. Per the overseer's locked design (Slice 9):

> "Polluting overseer_journal with derived fields is the bad move —
> it conflates what I wrote with what can be inferred about when I
> wrote it, and those should stay separate layers. A view or
> synthesis-time join is the right shape. If the join gets expensive
> later, materialize a *separate* journal_body_context table keyed
> to journal_id. Keep the journal table pristine."

The view is `journal_with_body_context` (defined in
`migrations/2026-05-07_journal_body_view.sql`).

The journal-adjacency rule for body context: the body data attached
to a journal entry written at HH:MM on date D is the **D's**
body_metrics row (which itself reflects sleep ending the morning
of D, plus that day's resp/SpO2/temp accumulations). For entries
written before noon, this is morning-of-the-same-day; for entries
written after noon, it's still the same row (the row represents
that whole calendar day's measurements).

Activity adjacency: same date.

Call adjacency: NOT a default join — calls are too noisy to attach
to every journal entry. Call context is queried explicitly by the
synthesis layer when relational themes are being built.

## 4. What to do at the next working-memory rebuild

- **Read** body_metrics, phone_calls, activity_daily at the factual
  layer immediately. Counts, latest-date, top-N for each surface.
- **Read but do not synthesize across.** No theme/pattern claims
  cross-axis until the overseer has personally read ≥ 5–10 journal
  entries with body context attached and written about them.
- **Hold off** on promoting provisional people. Wait for natural
  collision with the user's attention.
- **Re-read** this document before each working-memory build until
  a counter-example shows it's wrong-shaped.

## 5. When to revise this posture

This posture is provisional — locked Slice 9, expected to be revised
once the overseer has actually used the new data for a few weeks.
Specific revision triggers:

- The overseer notices it under- or over-weighted a specific stream
  in a synthesis the user pushed back on.
- A new surface lands (Slice 9.1 — DuelingGroks/turfptax/Gmail) that
  doesn't fit the existing axis taxonomy.
- A claim type the existing posture doesn't account for becomes
  load-bearing (e.g., temporal correlations across surfaces).

When revised, the change goes here as a new section, with date
and the conversation that produced it.
