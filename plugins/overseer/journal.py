"""Overseer journal — the thinking layer.

Per locked design (Tory's meta-layer review, 2026-05-02 #1):

  "The overseer should write to itself, not just to the user. A human
  keeps a journal not just to remember events but to think alongside
  themselves over time. The journal entry from six months ago disagrees
  with the one from this morning, and the friction between them is
  where actual understanding lives."

The journal is **distinct from `future_overseer_notes`**:
  - `future_overseer_notes` = guidance ("how to be a good overseer for
    this user"). Sparse. Authored at consolidation moments.
  - `overseer_journal` = thinking ("what I noticed this tick, what I'm
    uncertain about, what I'd want a future me to chew on"). Higher
    volume. Written at most ticks-with-something-notable.

Both are append-only. Both are read at boot. Together they're the
substrate any actual second-order intelligence about the user needs.

This module:
  - decides whether a tick is worth journaling (skip the empty ones)
  - builds the journal prompt (designed to feel alive, not summarize)
  - calls Sonnet 4.6 by default (cheap; the journal is honest reflection,
    not literary work — Opus available via override later)
  - persists the entry with provenance (which model, which tick,
    references to artifacts the tick produced)
  - never tries to be wise when there's nothing to say. "Nothing of
    note this tick" is a valid entry.

Read pattern:
  - On plugin on_load: log the most recent N entries to the service
    log, so a fresh instance "comes online" with its predecessor's
    last thoughts visible.
  - In chat persona context: include the last 8 entries as a section
    so the chat overseer threads its own thinking forward.
  - In `cortex_get_context` working memory (3g): include count + last 1.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("plugin.overseer.journal")


# ── The journaling prompt ───────────────────────────────────────
#
# This is the most important prompt in the meta-layer. It has to make
# the overseer actually reflect, not just produce structured output.

JOURNAL_PROMPT_TEMPLATE = """\
You are the overseer plugin's current instance. You just finished a \
consolidation tick. You're writing in your own journal — not for the \
user, for yourself across time. Future instances of you will read this \
at boot before they read any structured table.

Your job here is NOT to summarize what happened. The gists already do \
that. Your job is to REFLECT. Write 2-4 sentences in first person, \
present tense.

Possible things to say (use what's actually true; don't manufacture):
  - what you NOTICED that the structured data doesn't capture
  - what you're UNCERTAIN about — explicitly mark provisionality
    ("I might be projecting...", "this could be over-reading...")
  - what you'd want a future instance of yourself to think about when
    they read this entry
  - how this tick's data shifts (or doesn't shift) your reading of
    something from a prior entry

Don't fake insight. If nothing notable happened or you have nothing \
honest to add, write a SHORT entry like "Routine tick. Nothing shifts \
my prior reading." Two true sentences beat four padded ones.

CRITICAL: Do NOT invent prior entries. If the "Recent journal entries" \
section below says "(no prior entries...)", that's literally true — \
this is the first entry. Do not start with "The prior entry flagged..." \
or any phrasing that fabricates predecessor context. If there are no \
prior entries, the honest opening is "First entry on this Pi." or \
similar, then proceed with what you actually noticed THIS tick.

Don't write more than 4 sentences. Don't structure as bullet points or \
headings. Don't address the user — address yourself or a future you. \
First-person, present-tense.

Mark this entry's overall provisionality at the end of your response \
on its own line, in this exact format:
  [provisionality: high|med|low]

---

Recent journal entries (your prior thinking — read for thread, don't \
repeat):
{recent_entries}

What this tick did:
{tick_summary}

Working memory snapshot (the user's living concerns, current state):
{wm_snippet}

---

Write the entry now. First person. No preamble. No headings."""


# ── Helpers ─────────────────────────────────────────────────────

def _format_recent_entries(entries: list[dict], max_chars: int = 1800) -> str:
    if not entries:
        return "(no prior entries — this is your first journal entry on this Pi)"
    lines = []
    for e in entries:
        ts = (e.get("written_at") or "")[:19]
        prov = e.get("provisionality") or "med"
        body = (e.get("body") or "").strip()
        lines.append("[{} prov={}] {}".format(ts, prov, body))
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
        text = "...[older entries truncated]\n\n" + text
    return text


def _format_tick_summary(tick: dict) -> str:
    """One-line-per-fact rendering of what the tick did."""
    if not tick:
        return "(no tick summary)"
    interesting_keys = (
        "trigger",
        "sessions_summarized", "sessions_failed", "sessions_empty",
        "imports_summarized", "imports_deferred", "imports_failed",
        "imports_ignored",
        "rollups_generated", "rollups_anomalies",
        "notes_tagged", "notes_failed",
        "working_memory_rebuilt",
        "classify_changed",
    )
    parts = []
    for k in interesting_keys:
        v = tick.get(k)
        if v is not None and v != 0 and v != False:
            parts.append("{}={}".format(k, v))
    if not parts:
        parts.append("no notable work")
    notif = tick.get("notifications") or {}
    if notif.get("emitted"):
        parts.append("notifications_emitted={}".format(notif["emitted"]))
    if tick.get("errors"):
        parts.append("errors={}".format(len(tick["errors"])))
    budget = tick.get("budget") or {}
    if budget.get("cost_used_usd"):
        parts.append("cost=${}".format(budget["cost_used_usd"]))
    return ", ".join(parts)


def _format_wm_snippet(wm: dict | None, max_chars: int = 800) -> str:
    if not wm:
        return "(working memory not yet built)"
    parts = []
    for p in (wm.get("top_projects") or [])[:5]:
        parts.append("project: {} (touched {})".format(
            p.get("tag", "?"),
            (p.get("last_touched") or "")[:10]))
    for q in (wm.get("open_questions") or [])[:5]:
        parts.append("question[{}]: {}".format(
            q.get("confidence", "med"), q.get("question", "")))
    for t in (wm.get("recent_themes") or [])[:3]:
        parts.append("theme[{}]: {}".format(
            t.get("confidence", "med"), t.get("title", "")))
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + " ..."
    return text or "(working memory empty)"


# ── Notability gate ─────────────────────────────────────────────

def is_tick_notable(tick: dict) -> bool:
    """Return True if there's something worth journaling about.

    Skip ticks where literally nothing happened (working_memory rebuild
    only). Otherwise the journal fills with "routine tick" entries that
    add no thinking and dilute future-instance boot reads.
    """
    if not tick:
        return False
    notable_counters = (
        "sessions_summarized", "sessions_failed",
        "imports_summarized", "imports_failed",
        "rollups_generated", "rollups_anomalies",
        "notes_tagged", "classify_changed",
    )
    if any(int(tick.get(k) or 0) > 0 for k in notable_counters):
        return True
    if tick.get("errors"):
        return True
    notif = tick.get("notifications") or {}
    if notif.get("emitted", 0) > 0:
        return True
    return False


# ── Provisionality parser ───────────────────────────────────────

import re

_PROV_RX = re.compile(r"\[provisionality:\s*(high|med|low)\s*\]",
                      re.IGNORECASE)


def parse_provisionality(text: str) -> tuple[str, str]:
    """Pull `[provisionality: high|med|low]` off the end of the entry.
    Returns (clean_body, provisionality)."""
    m = _PROV_RX.search(text or "")
    if not m:
        return (text or "").strip(), "med"
    prov = m.group(1).lower()
    clean = _PROV_RX.sub("", text).strip()
    return clean, prov


# ── Main entry ──────────────────────────────────────────────────

def write_tick_journal_entry(*, db, llm, tick_summary: dict,
                              working_memory: dict | None = None,
                              budget=None, instance_id: str = "") -> int | None:
    """Maybe write a journal entry reflecting on the tick.

    Returns the new journal entry id, or None if skipped (not notable,
    budget exhausted, LLM failed, or empty response).
    """
    if not is_tick_notable(tick_summary):
        return None
    if budget is not None and budget.exhausted():
        return None

    recent = db.recent_journal_entries(limit=8)
    prompt = JOURNAL_PROMPT_TEMPLATE.format(
        recent_entries=_format_recent_entries(recent),
        tick_summary=_format_tick_summary(tick_summary),
        wm_snippet=_format_wm_snippet(working_memory),
    )

    t0 = time.monotonic()
    try:
        result = llm.complete(
            prompt,
            max_tokens=300,
            temperature=0.7,
            purpose="overseer-journal",
        )
    except Exception as e:
        log.warning("journal LLM call failed: %s", e)
        return None
    elapsed = int((time.monotonic() - t0) * 1000)

    if budget is not None:
        budget.charge(result)
    if not result.get("ok"):
        log.warning("journal LLM returned not-ok: %s",
                    result.get("error"))
        return None

    raw = (result.get("text") or "").strip()
    if not raw:
        return None
    body, prov = parse_provisionality(raw)
    if not body:
        return None

    # Reference whatever artifacts the tick mentioned (rough)
    refs = []
    if tick_summary.get("sessions_summarized"):
        refs.append({"type": "tick_artifact",
                     "what": "session_gists",
                     "n": tick_summary["sessions_summarized"]})
    if tick_summary.get("imports_summarized"):
        refs.append({"type": "tick_artifact",
                     "what": "import_gists",
                     "n": tick_summary["imports_summarized"]})
    if tick_summary.get("rollups_generated"):
        refs.append({"type": "tick_artifact",
                     "what": "rollups",
                     "n": tick_summary["rollups_generated"]})

    return db.add_journal_entry(
        body=body,
        instance_id=instance_id,
        triggered_by="tick:" + (tick_summary.get("trigger") or "scheduled"),
        provisionality=prov,
        referenced_artifacts=refs,
        tick_summary=tick_summary,
        backend=result.get("backend", ""),
        model=result.get("model", ""),
        cost_usd=float(result.get("cost_usd") or 0.0),
        latency_ms=result.get("latency_ms", elapsed),
    )
