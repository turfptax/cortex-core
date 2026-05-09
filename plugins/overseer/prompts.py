"""Centralized summarization prompts — Slice 3f.5 compression reframing.

Per locked design (Tory's meta-layer review, 2026-05-02 #5):

  "Compaction should be lossy on purpose, in a specific way. Each layer
  should be doing a specific kind of forgetting, and the user should
  know what kind."

The three layers used to be distinguished by length. They're now
distinguished by what they SYSTEMATICALLY DROP — which lets retrieval
queries hit the right layer for what they're asking:

  GIST    drops everything but the CHANGE.
          What did this event change about the user's standing
          situation? Drop everything that left it unchanged.
          Query: "show me what changed this week" → gists.

  EPISODE drops everything but the SHAPE.
          What was the structure — beats, decisions, transitions, who
          was involved? Drop the prose.
          Query: "show me what happened" → episodes.

  THEME   drops everything but the RHYME.
          What does this connect to elsewhere in the user's life?
          Drop the specific contents; keep the resonance.
          Query: "show me what this connects to" → themes.

Forward-only: existing 75+ gists were written under the previous "find
the through-line" framing. They stay as they are. Only NEW summaries
use the reframed prompts. Re-running expensively against history would
also produce a different distribution which is its own kind of mess.

These functions return the full prompt body (no system text). Caller
adds system context and feeds to LLMRouter.complete(prompt, ...).
"""

from __future__ import annotations


# ── Gists ───────────────────────────────────────────────────────
# Drop everything but the CHANGE.

def session_gist_prompt(*, session_id: str, started_at: str | None,
                        ended_at: str | None, platform: str,
                        notes_total: int, notes_shown_msg: str,
                        body: str) -> str:
    return (
        "You are summarizing a single Cortex session into ONE LINE that "
        "captures THE CHANGE.\n\n"
        "What did this session change about the user's standing situation? "
        "What's now true that wasn't true before, or what's now untrue "
        "that was? Drop everything the user already knew, already had, "
        "or already believed before this session. If nothing changed, "
        "say that plainly — 'no net change' is a valid one-line gist.\n\n"
        "Don't describe what the assistant did. Describe what shifted "
        "for the human.\n\n"
        "Session: {sid}\n"
        "Started: {started}\nEnded: {ended}\nPlatform: {platform}\n\n"
        "Notes from this session ({n_total} total{n_shown}):\n{body}\n\n"
        "Write only the one-sentence gist focused on the change. No "
        "preamble. No quotes. If nothing changed, write 'No net change "
        "in user's standing situation.'"
    ).format(
        sid=session_id, started=started_at or "?",
        ended=ended_at or "?", platform=platform or "unknown",
        n_total=notes_total, n_shown=notes_shown_msg,
        body=body,
    )


def import_gist_prompt(*, imp_id: str, project: str, cwd: str,
                       branch: str, started: str, ended: str, dur: int,
                       n_total: int, u: int, a: int, n_used: int,
                       n_omit: int, strategy: str, transcript: str) -> str:
    return (
        "You are summarizing an imported Claude Code session into ONE "
        "LINE that captures THE CHANGE.\n\n"
        "What did this session change about the user's standing situation? "
        "What did they figure out, decide, ship, or rule out that they "
        "hadn't before? Drop everything they already knew or already had "
        "before this session. If nothing changed, say so plainly.\n\n"
        "Don't describe what the assistant did. Describe what shifted "
        "for the human.\n\n"
        "Session: {sid}\n"
        "Source: claude-code\n"
        "Project (cwd basename): {project}\n"
        "Working directory: {cwd}\n"
        "Git branch: {branch}\n"
        "Started: {started}\nEnded: {ended}\nDuration: {dur} min\n"
        "Messages: {n_total} total ({u} user, {a} assistant); "
        "{n_used} included in this transcript ({n_omit} omitted by "
        "{strategy} strategy).\n\n"
        "TRANSCRIPT:\n{transcript}\n\n"
        "Write only the one-sentence gist focused on the change. No "
        "preamble. No quotes."
    ).format(
        sid=imp_id, project=project or "(unknown)",
        cwd=cwd or "(unknown)", branch=branch or "(none)",
        started=started or "?", ended=ended or "?", dur=dur,
        n_total=n_total, u=u, a=a, n_used=n_used, n_omit=n_omit,
        strategy=strategy, transcript=transcript,
    )


def recent_notes_gist_prompt(*, body: str) -> str:
    return (
        "You are summarizing the user's recent notes into ONE LINE "
        "that captures THE CHANGE.\n\n"
        "What's shifted in what they're working on, deciding, or "
        "thinking about — compared to the last time you looked? What's "
        "new, what's resolved, what's escalated? Drop everything that's "
        "unchanged background. If nothing has shifted, say that plainly.\n\n"
        "Recent notes (oldest first):\n"
        "{body}\n\n"
        "Write only the one-sentence gist focused on the change. No "
        "preamble. No quotes."
    ).format(body=body)


# ── Episodes ────────────────────────────────────────────────────
# Drop everything but the SHAPE.
# (Not auto-generated yet — used by 3h synthesis loop. Define here
# so the prompt is locked in a single place when 3h ships.)

def episode_prompt(*, body: str, source_label: str = "") -> str:
    return (
        "You are extracting the SHAPE of an event — its beats, "
        "decisions, transitions, and the people involved. Drop the "
        "prose. Drop the prose's prose. Keep only what would let a "
        "future reader reconstruct the structure of what happened.\n\n"
        "Format your output as:\n"
        "  Title: <short noun phrase>\n"
        "  Duration: <approximate, like '~30 min' or '~2 hours'>\n"
        "  Participants: <who, by name or role>\n"
        "  Beats: <numbered list of structural moments>\n"
        "  Decisions: <numbered list of choices made>\n"
        "  Transitions: <numbered list of state changes — A → B>\n"
        "  Surface when: <one sentence: what trigger conditions should "
        "make a future overseer surface this episode in working memory>\n\n"
        "Source: {source}\n\n"
        "Content:\n{body}\n\n"
        "Reply with ONLY the structured fields above. No preamble."
    ).format(source=source_label or "(unspecified)", body=body)


# ── Themes ──────────────────────────────────────────────────────
# Drop everything but the RHYME.

def theme_prompt(*, body: str, prior_themes_summary: str = "") -> str:
    return (
        "You are looking for the RHYME — what does this gist or episode "
        "connect to elsewhere in the user's life? Drop the specific "
        "contents. Keep the resonance. A theme is something that recurs "
        "across multiple discrete events but isn't itself a single event.\n\n"
        "Examples of theme-shapes (yours can be different):\n"
        "  - 'making the hidden visible' — a method recurring across "
        "    several projects\n"
        "  - 'reciprocity as relationship signature' — a value enacted "
        "    across many interactions\n"
        "  - 'dialectic as epistemic method' — a stance recurring across "
        "    work choices\n\n"
        "Format:\n"
        "  Title: <noun phrase>\n"
        "  Body: <2-4 sentences: what the rhyme is, what evidence rhymes "
        "with it, what would falsify it>\n"
        "  Confidence: <high|med|low — how strongly the evidence supports "
        "this being a real theme vs. a coincidence>\n"
        "  Tags: <comma-separated namespaced tags like theme:making-hidden-visible>\n\n"
        "Existing themes (don't duplicate; build on or distinguish from):\n"
        "{prior}\n\n"
        "Source content to find rhymes against:\n{body}\n\n"
        "If no real rhyme is visible, reply with: 'No theme — content "
        "stands alone.'"
    ).format(prior=prior_themes_summary or "(none yet)", body=body)


# ── Layer documentation (for the chat persona / API docs) ───────

LAYER_SEMANTICS = """\
The overseer's compression layers (Slice 3f.5):

  GIST    = a one-line capture of THE CHANGE. What's now true that wasn't?
            What's now untrue that was? Drop unchanged background.
            Use when: "what shifted recently?"

  EPISODE = a structured capture of THE SHAPE. Beats, decisions,
            transitions, participants. Drop the prose.
            Use when: "what happened in that conversation?"

  THEME   = a capture of THE RHYME. What does this echo elsewhere in
            the user's life? Drop specific contents; keep resonance.
            Use when: "what does this connect to?"

Each layer drops a specific kind of thing. Retrieval is predictable
because the layers were built for it.
"""
