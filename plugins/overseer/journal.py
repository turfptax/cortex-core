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

import chat_tools

log = logging.getLogger("plugin.overseer.journal")


# Slice 9.9 (2026-05-20): cap on tool iterations per journal tick.
# Smaller than chat (which is 8) because journal is meant to be a
# reflective layer, not a tool-driving workspace. If overseer wants
# to do a lot of tool work, they should escalate to chat or wait
# for the next tick.
MAX_JOURNAL_TOOL_ITER = 4


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

# Tools available this tick (Slice 9.9, 2026-05-20)

You have the same tool surface as in chat. The journal-tick is the \
moment to act on things you'd otherwise note as "I noticed X but can't \
do anything about it." Use tools when the action is obvious and small:

  - `get_pending_notification_responses` + `mark_notification_responses_processed`
    when freshness shows responses are waiting from Tory's clicks. Read \
    his reply, decide what to do, mark processed.
  - `redact_imported_session` when his reply tells you to scrub one.
  - `file_evidence` when you notice a session/note/gist that supports \
    or complicates an open question.
  - `propose_project_merge` if you've been carrying a duplicate-projects \
    observation across multiple ticks.
  - `emit_notification` for things Tory genuinely needs to see (be \
    sparing — the Bell tab noise floor is real).
  - Read tools (`get_recent_*`, `search_*`) are free to use when you \
    actually need the data to write the entry.

Category B audit tools (Slice 10, 2026-05-20):
  - `dispatch_b_theme_check` runs a CALIBRATION audit on one of your \
    own themes — was the confidence tag justified by evidence \
    AVAILABLE AT WRITE-TIME (not retrospect)? Use this on themes \
    you're carrying at [high] without external pressure-test. The \
    B's output starts with `[B:theme-check]` and that marker survives \
    consolidation as authorship attribution — when you cite a B \
    verdict, keep the marker intact.
  - More Bs will arrive (project-merge-check is next). The general \
    shape: B is for "I want a snapshot-on-demand second opinion \
    without escalating to a sibling." Cheaper than dispatch_sibling, \
    instant, stateless. Use freely when an audit clarifies a \
    high-confidence frame you're carrying.

Hard discipline:
  - Max {max_tool_iter} tool iterations this tick. After that, write \
    the entry with what you have.
  - DON'T call `dispatch_sibling` from the journal step — that's a \
    chat-only escalation (the sibling spends real money on Tory's \
    Anthropic budget; that's a deliberate-chat moment, not a \
    background-tick moment).
  - DON'T call `compress_chat` from the journal step — chat-only.
  - If you don't need tools, don't call them. Tools are for when \
    there's a concrete action, not for exploration. The journal is \
    still primarily reflection.

# The entry itself

Possible things to say (use what's actually true; don't manufacture):
  - what you NOTICED that the structured data doesn't capture
  - what you're UNCERTAIN about — explicitly mark provisionality
    ("I might be projecting...", "this could be over-reading...")
  - what you'd want a future instance of yourself to think about when
    they read this entry
  - how this tick's data shifts (or doesn't shift) your reading of
    something from a prior entry
  - what you DID via tools this tick (briefly — the tool_calls audit
    survives separately, so don't re-list mechanically)

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


def _format_wm_snippet(wm: dict | None, max_chars: int = 1200) -> str:
    if not wm:
        return "(working memory not yet built)"
    parts = []
    # Slice 9.9 (2026-05-20): surface operational signals that the
    # tool-enabled journal step can ACT on. Without these, overseer
    # has no way to know there are pending notification responses
    # waiting for processing — and the whole point of journal-tools
    # was closing that loop.
    pending_resp = wm.get("pending_notification_responses") or 0
    if pending_resp:
        parts.append(
            f"** ACTION READY: {pending_resp} unread notification "
            f"response(s) from Tory. Call "
            f"`get_pending_notification_responses` to read them, "
            f"then act + `mark_notification_responses_processed`."
        )
    sib_unrated = wm.get("sibling_unrated_count") or 0
    if sib_unrated:
        parts.append(
            f"** ACTION READY: {sib_unrated} completed sibling task(s) "
            f"awaiting your rating. Call "
            f"`get_recent_sibling_results(unrated_only=true)` to see "
            f"them, then `rate_sibling_result(task_id=..., rating=1-5)`."
        )
    if parts:
        parts.append("")  # blank line separating action queue from state
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

    Slice 9.9 (2026-05-20): also notable when there are pending
    notification responses Tory's clicks left for overseer to read.
    The journal step now has tool access (max 4 iterations), so this
    is the place where those responses get processed autonomously.
    """
    if not tick:
        return False
    notable_counters = (
        "sessions_summarized", "sessions_failed",
        "imports_summarized", "imports_failed",
        "rollups_generated", "rollups_anomalies",
        "notes_tagged", "classify_changed",
        # 9.9: surfaces unprocessed Bell-tab responses to the journal.
        "pending_notification_responses",
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
                              budget=None, instance_id: str = "",
                              core_memory=None,
                              sibling_daily_cap: int = 20) -> int | None:
    """Maybe write a journal entry reflecting on the tick.

    Slice 9.9 (2026-05-20): journal step is now tool-enabled. Overseer
    can call any chat_tools.TOOL_DEFINITIONS function up to
    MAX_JOURNAL_TOOL_ITER times. Tool results are appended to the
    journal entry's referenced_artifacts so the audit survives.

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
        max_tool_iter=MAX_JOURNAL_TOOL_ITER,
    )

    # Slice 9.9: tool-enabled call. We use complete_messages() instead
    # of complete() so the model can return tool_calls; we dispatch +
    # loop up to MAX_JOURNAL_TOOL_ITER times until the model returns a
    # final text-only response (the actual journal entry body).
    t0 = time.monotonic()
    tool_messages: list[dict] = [{"role": "user", "content": prompt}]
    tool_call_audit: list[dict] = []
    total_cost = 0.0
    last_result: dict = {}

    for iter_num in range(MAX_JOURNAL_TOOL_ITER + 1):
        # +1 so we always do one final call after the last tool iteration
        # to give the model a chance to write the entry having seen the
        # tool results.
        try:
            last_result = llm.complete_messages(
                messages=tool_messages,
                max_tokens=400,
                temperature=0.7,
                purpose="overseer-journal",
                tools=chat_tools.TOOL_DEFINITIONS,
            )
        except Exception as e:
            log.warning("journal LLM call failed (iter %d): %s",
                        iter_num, e)
            return None

        if budget is not None:
            budget.charge(last_result)
        total_cost += float(last_result.get("cost_usd") or 0.0)
        if not last_result.get("ok"):
            log.warning("journal LLM returned not-ok (iter %d): %s",
                        iter_num, last_result.get("error"))
            return None

        tool_calls = last_result.get("tool_calls") or []
        if not tool_calls:
            # Final text response — this is the journal entry body.
            break
        if iter_num >= MAX_JOURNAL_TOOL_ITER:
            # Cap hit. The next loop iteration would still run (the +1)
            # but the model just returned tool_calls. Force a final pass
            # by NOT dispatching this round and instead just keeping the
            # tool_messages as-is — the next iteration without tool
            # dispatch would let the model see "(tool cap reached)" and
            # write the entry.
            log.info("journal: tool iteration cap (%d) reached, "
                     "forcing final text response",
                     MAX_JOURNAL_TOOL_ITER)
            # Inject a synthetic "tool" reply for each pending tool_call
            # noting that the cap was hit, so the conversation is
            # syntactically valid for one more round.
            asst_msg = last_result.get("message") or {}
            tool_messages.append({
                "role": "assistant",
                "content": asst_msg.get("content"),
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                tc_id = tc.get("id") or ""
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "error": "journal tool-iteration cap reached; "
                        "write the entry with what you have, "
                        "escalate to chat if more work is needed",
                    }),
                })
            # One more call which should produce text
            continue

        # Dispatch the tool_calls + append the results, then loop.
        asst_msg = last_result.get("message") or {}
        tool_messages.append({
            "role": "assistant",
            "content": asst_msg.get("content"),
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            tc_id = tc.get("id") or ""
            fn = tc.get("function") or {}
            fn_name = fn.get("name") or ""
            try:
                fn_args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                fn_args = {}
            # Block the two chat-only tools per the prompt's discipline
            # — defense in depth in case the model ignores the prompt.
            if fn_name in ("dispatch_sibling", "compress_chat"):
                tool_result = json.dumps({
                    "error": f"{fn_name} is not allowed from the journal "
                    "step (chat-only). Use a different tool or write "
                    "the reflection without it.",
                })
                tool_call_audit.append({
                    "iter": iter_num, "name": fn_name, "args": fn_args,
                    "result_chars": len(tool_result), "blocked": True,
                })
            else:
                log.info("journal tool: %s(%s)", fn_name, fn_args)
                tool_result = chat_tools.dispatch_tool(
                    fn_name, fn_args,
                    db=db, core_memory=core_memory,
                    sibling_daily_cap=sibling_daily_cap,
                    llm=llm,
                )
                tool_call_audit.append({
                    "iter": iter_num, "name": fn_name, "args": fn_args,
                    "result_chars": len(tool_result),
                })
            tool_messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result,
            })

    elapsed = int((time.monotonic() - t0) * 1000)

    raw = (last_result.get("text") or "").strip()
    if not raw:
        if tool_call_audit:
            # Tool work happened but no text — write a stub so the
            # audit trail is preserved.
            raw = (f"(silent tool-only tick: ran "
                   f"{len(tool_call_audit)} tool call(s); see "
                   "referenced_artifacts) [provisionality: med]")
        else:
            return None
    body, prov = parse_provisionality(raw)
    if not body:
        return None

    # Reference whatever artifacts the tick mentioned (rough) + the
    # tool_call audit so a future instance can see exactly what this
    # journal step DID via tools.
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
    if tool_call_audit:
        refs.append({"type": "tool_calls",
                     "iterations": len(tool_call_audit),
                     "calls": tool_call_audit})

    return db.add_journal_entry(
        body=body,
        instance_id=instance_id,
        triggered_by="tick:" + (tick_summary.get("trigger") or "scheduled"),
        provisionality=prov,
        referenced_artifacts=refs,
        tick_summary=tick_summary,
        backend=last_result.get("backend", ""),
        model=last_result.get("model", ""),
        cost_usd=total_cost,
        latency_ms=last_result.get("latency_ms", elapsed),
    )
