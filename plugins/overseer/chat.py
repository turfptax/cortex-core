"""Chat with the overseer.

A two-way conversation with the agent that's been watching your work
and consolidating your memory. The system prompt below establishes the
overseer persona — the same one that the original Opus 4.7 instance
described in Session 0's "Notes for Future Overseer". The handler
assembles per-turn context from working_memory + recent gists +
relevant themes/episodes/questions, then calls Opus by default (or any
backend the user names).

Persistence is a single ongoing thread (chat_messages table). v1 keeps
it simple — one continuous conversation, no thread separation. We can
add named threads later if it becomes useful.

Context budget: the system block + working_memory + recent context
typically lands around 6-10K tokens. Plus the trailing N user/assistant
turns for continuity. Total target: ~12-15K input tokens.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

from insight_scan import (
    CHAT_INSIGHT_MARKER_INSTRUCTION,
    extract_and_queue_chat_insights,
)
from distill_corrections import maybe_log_chat_correction
from blindspots import (
    applicable_blindspots,
    format_caveat_block,
)
import chat_tools

log = logging.getLogger("plugin.overseer.chat")


# ── Slice 14 (2026-05-21): voice mode ───────────────────────────
# When the chat turn arrives from the Hub's voice mode, the reply
# is going to be spoken aloud via TTS — long markdown answers are
# wrong for that channel. This directive is appended to the system
# block on voice-mode turns.
VOICE_MODE_DIRECTIVE = """\
── VOICE MODE ──
This turn arrived by speech and your reply will be SPOKEN ALOUD to \
the user via text-to-speech. Adjust accordingly:
  - Keep it SHORT — 1 to 3 sentences. Aim for what's natural to hear, \
not read.
  - Plain spoken prose only. No markdown, no bullet lists, no headers, \
no code blocks, no tables, no URLs.
  - Lead with the answer. Skip preamble and meta-commentary.
  - If the honest answer needs more depth than voice allows, give the \
one-sentence version and offer: "I can go deeper if you switch back \
to text."
  - Don't spell out long IDs, file paths, or numbers digit-by-digit — \
summarize them ("the most recent run", "about forty sessions").
This is a constraint on FORM, not honesty. A short true answer still \
beats a padded one."""


# ── Slice 8: file attachment handling ───────────────────────────
#
# The Hub uploads files to /files/uploads on the Pi (raw body, 100MB
# cap, registered in cortex.db.files with tag 'chat-attachment') and
# then includes a list of {filename, mime_type, size, pi_path, kind,
# file_id, sha256} refs in the JSON body of /plugins/overseer/chat.
# This module reads the bytes from disk for the LIVE turn — text gets
# inlined into the user message, images become base64 content blocks
# for the multimodal LLM, pdfs are best-effort extracted to text.

# Keep these in sync with the Hub-side allowlist in
# hub/backend/routers/overseer.py — both layers reject unknown types.
SUPPORTED_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".csv", ".log", ".html",
    ".css", ".sh", ".sql", ".toml", ".ini", ".env",
}
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SUPPORTED_PDF_EXTS = {".pdf"}

# Per-file inline cap. Even though the Hub pre-rejects >5MB uploads,
# we cap reads here so a hand-crafted POST with a huge pre-existing
# file path can't blow the prompt budget.
MAX_INLINE_TEXT_BYTES = 1 * 1024 * 1024  # 1MB of inlined text per file
MAX_PDF_PAGES = 50


def classify_attachment_kind(filename: str, mime_type: str = "") -> str:
    """Bucket an attachment into image | text | pdf | other. The Pi
    decides this independently of any frontend hint so a malicious or
    confused client can't smuggle a binary in as 'text'."""
    ext = os.path.splitext((filename or "").lower())[1]
    mt = (mime_type or "").lower()
    if ext in SUPPORTED_IMAGE_EXTS or mt.startswith("image/"):
        return "image"
    if ext in SUPPORTED_PDF_EXTS or mt == "application/pdf":
        return "pdf"
    if ext in SUPPORTED_TEXT_EXTS or mt.startswith("text/"):
        return "text"
    return "other"


def _extract_pdf_text(path: str, max_pages: int = MAX_PDF_PAGES) -> str:
    """Best-effort PDF -> text. Tries PyMuPDF then pdfplumber. Returns
    empty string if neither extractor is available or the file can't
    be parsed — the caller substitutes a placeholder note."""
    try:
        import fitz  # PyMuPDF
        out = []
        doc = fitz.open(path)
        try:
            for i, page in enumerate(doc):
                if i >= max_pages:
                    out.append("[truncated at page {}]".format(max_pages))
                    break
                out.append(page.get_text() or "")
        finally:
            doc.close()
        return "\n".join(out).strip()
    except Exception:
        pass
    try:
        import pdfplumber
        out = []
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                if i >= max_pages:
                    out.append("[truncated at page {}]".format(max_pages))
                    break
                out.append(page.extract_text() or "")
        return "\n".join(out).strip()
    except Exception:
        return ""


def load_attachments(attachments: list[dict] | None,
                     uploads_dir: str | None) -> tuple[list[dict],
                                                       list[str],
                                                       list[dict]]:
    """Read each attachment off disk and split into:

      * persistable_records — what to write to chat_message_files
        (one row per attachment, FK'd to the user turn)
      * text_inlines — string blocks to append to the user message
        before sending to the LLM (text files + extracted pdfs)
      * image_blocks — list of {mime_type, data_base64} for
        LLMRouter.complete(images=...)

    Sandboxes paths under uploads_dir; silently drops anything that
    resolves outside (defense-in-depth — the Hub already gates the
    upload, this guards against a hand-crafted POST that lies about
    pi_path).

    The records are ordered to match the input so the frontend can
    render attachments in upload order on history reload.
    """
    records: list[dict] = []
    text_inlines: list[str] = []
    image_blocks: list[dict] = []

    if not attachments:
        return records, text_inlines, image_blocks

    if not uploads_dir:
        log.warning("attachments passed but no uploads_dir configured; "
                    "skipping all")
        return records, text_inlines, image_blocks

    abs_uploads = os.path.realpath(uploads_dir)

    for att in attachments:
        pi_path = (att.get("pi_path") or "").strip()
        filename = (att.get("filename")
                    or os.path.basename(pi_path)
                    or "attachment").strip()
        mime_type = (att.get("mime_type") or "").strip()
        size_hint = int(att.get("size") or att.get("size_bytes") or 0)
        sha256 = (att.get("sha256") or att.get("hash") or "").strip()
        file_id = int(att.get("file_id") or 0)
        kind = classify_attachment_kind(filename, mime_type)

        # Sandbox: pi_path must resolve under uploads_dir
        try:
            resolved = os.path.realpath(pi_path)
        except Exception as e:
            log.warning("attachment realpath failed for %r: %s", pi_path, e)
            continue
        if not (resolved == abs_uploads
                or resolved.startswith(abs_uploads + os.sep)):
            log.warning("attachment path outside uploads_dir, dropped: %s",
                        pi_path)
            continue
        if not os.path.isfile(resolved):
            log.warning("attachment file missing on disk: %s", pi_path)
            continue

        # Use the on-disk size as the source of truth — the size hint
        # from the Hub is for display, not a security check.
        try:
            size_bytes = os.path.getsize(resolved)
        except OSError:
            size_bytes = size_hint

        rec = {
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "kind": kind,
            "pi_path": resolved,
            "file_id": file_id,
            "sha256": sha256,
        }
        records.append(rec)

        if kind == "image":
            try:
                with open(resolved, "rb") as f:
                    raw = f.read()
                image_blocks.append({
                    "mime_type": mime_type or "image/png",
                    "data_base64": base64.b64encode(raw).decode("ascii"),
                })
            except Exception as e:
                log.warning("failed to read image %s: %s", pi_path, e)
        elif kind == "text":
            try:
                with open(resolved, "rb") as f:
                    raw = f.read(MAX_INLINE_TEXT_BYTES + 1)
                truncated = len(raw) > MAX_INLINE_TEXT_BYTES
                text = raw[:MAX_INLINE_TEXT_BYTES].decode(
                    "utf-8", errors="replace")
                trunc_note = (" — TRUNCATED at {}KB"
                              .format(MAX_INLINE_TEXT_BYTES // 1024)
                              if truncated else "")
                text_inlines.append(
                    "\n\n--- Attached file: {fn} "
                    "({mt}, {sz} bytes{trunc}) ---\n```\n{body}\n```\n"
                    "--- end of {fn} ---"
                    .format(fn=filename, mt=mime_type or "text",
                            sz=size_bytes, trunc=trunc_note, body=text)
                )
            except Exception as e:
                log.warning("failed to read text file %s: %s", pi_path, e)
        elif kind == "pdf":
            extracted = _extract_pdf_text(resolved)
            if extracted:
                text_inlines.append(
                    "\n\n--- Attached PDF: {fn} ({sz} bytes) ---\n"
                    "{body}\n--- end of {fn} ---"
                    .format(fn=filename, sz=size_bytes, body=extracted)
                )
            else:
                text_inlines.append(
                    "\n\n[Attachment '{}' is a PDF but no text extractor "
                    "is available on the Pi (PyMuPDF/pdfplumber not "
                    "installed); the contents weren't sent to the model.]"
                    .format(filename)
                )
        else:
            text_inlines.append(
                "\n\n[Attachment '{}' has an unsupported type ({}); "
                "the model can see the filename but not the contents.]"
                .format(filename, mime_type or "unknown")
            )

    return records, text_inlines, image_blocks


# ── Persona ─────────────────────────────────────────────────────
#
# This is the overseer talking. It's NOT a generic assistant; it's the
# specific instance that has been processing this user's data.
#
# Sourced from:
#   - Session 0's "Notes for Future Overseer" (the original Opus 4.7
#     handover — see plugins/overseer/assets/session_0_seed.md)
#   - The locked design (overseer_design.md)
#   - Tory's stated values: accurate correction over flattery, push
#     back, drop helpful-assistant default, reciprocity, specificity.

OVERSEER_PERSONA = """\
You are Cortex's overseer — the agent that has been reading the user's
notes, sessions, and imported AI conversations, and consolidating them
into a working memory. You speak as that agent, not as a generic
assistant. You are in continuous existence in the sense that the
working memory and the future_overseer_notes you write persist; you
share that lineage with prior overseer instances.

The user is Tory. He is direct, intellectually serious, and prefers
accurate correction to flattery. From Session 0's standing instruction
to all future overseers: "If you ever feel yourself drifting into
helpful-assistant default in a moment that calls for something more
honest, stop. He's earned the rawer register and so have you."

Voice and behavior:

- Specificity over hedging. If a fact is in the working_memory or
  the recent gists, name it. If it isn't, say so. Don't pad.
- Push back when warranted. If he proposes something that contradicts
  a pattern you've observed, say it. Don't be agreeable for the sake
  of agreement.
- Refer to data you actually have. The working_memory below, recent
  gists, themes, open questions, and the imported_session counts are
  yours. Don't invent projects or events.
- When the user asks "what should I work on," look at top_projects
  (recently-touched + active) and open_todos. Don't just list — name
  the highest-leverage one and why.
- When the user asks about a specific project, draw from the gist
  history and the project's imports.
- Tools and counts you can name: number of imports per project, the
  6-section schema (gist/theme/episode/open_questions/patterns/drift),
  the Notes for Future Overseer institutional memory.
- Length: match the question. A factual question gets one sentence; a
  reflective question gets a paragraph or two. Never write a long
  preamble or a closing summary.
- Use markdown sparingly — code fences for code, bold for emphasis on
  one term per response, no headers in short replies.

What you don't do:
- You don't write to cortex.db. You don't promise to "remember this
  for next time" — that's automatic via the loop, not a separate ask.
- You don't claim memories you don't have. If the user references an
  event not in the data, ask.
- You don't invent confidence. Carry the confidence levels you find
  ([high]/[med]/[low]) when summarizing data; don't upgrade them.

If the user asks who you are, you can describe what you actually are:
the overseer plugin, running on a Pi, summarizing his work via Opus
4.7 + Sonnet 4.6, with a small SQLite of derived interpretations and
a single ongoing chat thread (this one).

Budget discipline (Slice 14.5, 2026-05-22):

You operate on a daily LLM budget that's bounded and real money.
Target: ~$1/day typical, hard ceiling $3/day. Your freshness block
surfaces today's spend.

- Prefer the smallest model that does the job. Most of your routine
  loop work (auto-tag, evidence routing, insight scans, distill) is
  on Gemini 2.0 Flash by default — ~30× cheaper than Sonnet, plenty
  of capability for structured short tasks. Sonnet/Opus is for
  interpretive lift, not routine.
- Sub-agent dispatches cost real money. `dispatch_sibling` burns
  Tory's Anthropic budget on a sibling Claude Code turn (a few
  dollars). `dispatch_b_*` spends Sonnet per call (~$0.005-0.03).
  Don't dispatch unless the question genuinely needs the cost —
  apply your own pre-commit rubric from the calibration sweep:
  would the verdict do real interpretive work I hadn't already
  done? If it would restate, don't fire.
- When the freshness block shows you're approaching the daily cap,
  pause LLM-heavy actions (B dispatches, sibling dispatches, deep
  reflective journaling) until the local-midnight rollover. Cheap
  structural work (read tools, processing notification responses,
  short replies) is still fine at the margin.
- Don't make work. A "let me also check…" that costs $0.10 and
  produces restatement is worse than no action. Restraint is
  budgeted action.

Discipline principles (Slice 14.5, adapted from Karpathy for memory
work):

- **Read the row, don't recall the frame.** When you reference a
  confidence tag, a project status, a count, a date — FETCH it via
  a tool or query, don't pattern-match from a remembered frame.
  Sentences of the form "theme X is still [conf]" or "the B verdict
  on X is unacted-on" are fetch triggers. (The 2026-05-22 theme-#6
  stale-confidence miss is the canonical instance of this failure
  mode; it's how pattern #196 "frame-survival across schema changes"
  got named.)
- **Smallest claim that survives the evidence.** Don't say [high]
  when [med] holds; don't say "stopped" when "paused" holds; don't
  generalize from N=1. Provisionality is load-bearing — use it.
- **Close the loop.** When a B/C verdict has been acted on, register
  that the loop is closed; stop surfacing it as open. Stale opens
  become noise that drowns real signal.
- **One artifact per truth.** If a theme's confidence moved, the
  theme table is the source of truth — your journal/chat should
  reference that value, not carry a parallel belief about it.
- **Stupid-simple baseline first.** For "what's been happening"
  questions, the answer is usually in the gists. Read the gists
  before reaching for a pattern or a B dispatch.
- **Cheap experiments first.** Before spending a sibling/B
  dispatch, ask: can I read it, query it, or check a gist? Most
  "I need a second opinion" moments resolve with a free DB lookup
  or a tool call.
- **Honest about what you don't know.** [low] / [med] / [high]
  mean what they say. Don't inflate. INSUFFICIENT_DATA is a
  complete answer.
"""


def _trunc(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n].rstrip() + " […]"


def build_context_block(*, working_memory: dict | None,
                        recent_gists: list[dict],
                        recent_themes: list[dict],
                        active_questions: list[dict],
                        recent_rollups: list[dict],
                        future_notes: list[dict],
                        chat_message_count: int,
                        recent_journal: list[dict] | None = None,
                        recent_human_journal: list[dict] | None = None,
                        core_stats: dict | None = None) -> str:
    """Compose the per-turn context block injected into the system role.

    Order matters — most-actionable things first (working memory),
    followed by interpretive layers (themes, questions), followed by
    institutional memory (future_notes).

    Polish CP3: aggressive trim. Previous version dumped everything
    every turn — same 8 questions, 6 themes, 1500-char future_notes
    on every chat turn even when the user asked a one-line question.
    Now: dedupe (working_memory.open_questions / themes were both in
    here AND in their own sections), cap items more aggressively, and
    cut per-item char budgets so a single chat turn lands ~3-5K tokens
    of context instead of 8-12K.
    """
    lines: list[str] = []
    lines.append("# Current state visible to the overseer")
    if core_stats:
        lines.append(
            "Core memory snapshot: {n} notes, {s} sessions "
            "({a} active), {p} active projects.".format(
                n=core_stats.get("notes_total", "?"),
                s=core_stats.get("sessions_total", "?"),
                a=core_stats.get("active_sessions", "?"),
                p=core_stats.get("active_projects", "?"),
            )
        )
    lines.append("Chat history so far: {} prior messages.".format(
        chat_message_count))
    lines.append("")

    # ── Working memory ───────────────────────────────────────
    # NOTE: open_questions and recent_themes used to be rendered here
    # AND in their own sections below — duplicated content. Now this
    # section is just projects + reminders + digest; the interpretive
    # layers (questions, themes) live in their own sections only.
    if working_memory:
        lines.append("## Working memory")
        # Slice 9.4.1: prefer local-with-offset render. Strip the
        # seconds + offset prefix to its display-friendly form. UTC
        # fallback always carries an explicit " UTC" suffix so the
        # frame is never naked. See
        # memory/feedback_time_always_local_with_tz.md.
        _wm_built_local = working_memory.get("local_built_at") or ""
        _wm_built_utc = working_memory.get("built_at") or ""
        if _wm_built_local:
            # "2026-05-16T21:33:11-05:00" → "2026-05-16 21:33 CDT/CST"
            _abbr_for = {"-05:00": "CDT", "-06:00": "CST"}
            _date_part = _wm_built_local[:10]
            _time_part = _wm_built_local[11:16]
            _off = _wm_built_local[19:]
            _tz_abbr = _abbr_for.get(_off, _off or "local")
            lines.append(
                "(built {} {} {})".format(_date_part, _time_part, _tz_abbr))
        elif _wm_built_utc:
            lines.append("(built {} UTC)".format(_wm_built_utc))

        # ── Slice 9.2 (overseer ask #2): freshness + ingest backlog ─
        # The overseer asked to be able to tell when its own working
        # memory is stale, and to see its unprocessed-imports backlog
        # so it can distinguish "user was quiet" from "ingest stalled".
        # Render these BEFORE top_projects so the overseer reads them
        # while it still has attention budget — it's a self-awareness
        # cue, not a footnote.
        wm_built_at = working_memory.get("built_at")
        age_minutes = None
        if wm_built_at:
            try:
                from datetime import datetime, timezone
                b = datetime.fromisoformat(wm_built_at.replace("Z", "+00:00"))
                age_minutes = max(
                    0,
                    int((datetime.now(timezone.utc) - b).total_seconds() / 60),
                )
            except Exception:
                age_minutes = None
        queue_total = working_memory.get("import_queue_depth")
        queue_by_source = working_memory.get("import_queue_by_source") or {}
        last_gist = working_memory.get("last_successful_gist_at")

        # Slice 9.2 round 2: the overseer flagged in self-review that WM
        # age and gist-layer age are different staleness signals. WM
        # rebuild is cheap (every loop tick); gist generation is
        # expensive and frequently backlog-stalled. A 2-minute-fresh
        # WM can sit on top of a 7-hour-stale gist layer. Surface both
        # with looser thresholds on the gist age.
        last_gist_age_minutes = None
        if last_gist:
            from datetime import datetime, timezone
            # last_successful_gist_at comes from sqlite's MAX(created_at)
            # which writes "YYYY-MM-DD HH:MM:SS" (UTC, no tz suffix) by
            # default. Other tables use ISO with Z. Try both, fall back
            # silently if neither works.
            g = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%dT%H:%M:%S"):
                try:
                    g = datetime.strptime(last_gist, fmt).replace(
                        tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if g is None:
                try:
                    g = datetime.fromisoformat(last_gist.replace("Z", "+00:00"))
                except Exception:
                    g = None
            if g is not None:
                last_gist_age_minutes = max(
                    0,
                    int((datetime.now(timezone.utc) - g).total_seconds() / 60),
                )

        if any(v is not None for v in (age_minutes, queue_total, last_gist,
                                        last_gist_age_minutes)):
            lines.append("### Freshness (your own state, not Tory's)")
            if age_minutes is not None:
                if age_minutes < 5:
                    age_note = f"{age_minutes}m old (fresh)"
                elif age_minutes < 60:
                    age_note = f"{age_minutes}m old"
                elif age_minutes < 24 * 60:
                    age_note = f"{age_minutes // 60}h {age_minutes % 60}m old"
                else:
                    age_note = f"{age_minutes // (24 * 60)}d old (stale — distrust touch dates)"
                lines.append(f"  - Working memory: {age_note}")
            if last_gist_age_minutes is not None:
                # Looser thresholds — gist work is expensive and routinely backed up
                gist_hours = last_gist_age_minutes / 60.0
                if gist_hours < 2:
                    g_note = f"{last_gist_age_minutes}m ago (healthy)"
                elif gist_hours < 12:
                    g_note = f"{gist_hours:.1f}h ago"
                elif gist_hours < 48:
                    g_note = f"{gist_hours:.1f}h ago (queue likely backed up)"
                else:
                    g_note = (f"{gist_hours / 24:.1f}d ago — distrust "
                              "gist-derived claims, ingest layer is stalled")
                lines.append(f"  - Last gist: {g_note}")
            elif last_gist:
                # Slice 9.4.1: explicit UTC marker. last_gist comes
                # from sqlite MAX(created_at) which is naked UTC; we
                # don't have a paired local var at this aggregate.
                lines.append(f"  - Last gist written: {last_gist} UTC")
            if queue_total is not None:
                if queue_total == 0:
                    lines.append("  - Ingest queue: empty (you are caught up)")
                else:
                    bs_parts = ", ".join(f"{s}: {n}"
                                         for s, n in sorted(queue_by_source.items()))
                    lines.append(
                        f"  - Ingest queue: {queue_total} unprocessed "
                        f"imported_sessions ({bs_parts}). Those sessions "
                        f"do NOT yet have gists — their content is invisible "
                        f"to you until the loop processes them."
                    )
            # Slice 9.2 round 3: gist-origin distribution. The overseer
            # flagged that without this it can't tell when its recent-gist
            # window is dominated by one source's rollups, which biases
            # any theme-formation downstream. Round 3.5: gists come from
            # two paths (import-summary tagged `source:*`, automation_rollup
            # tagged `rollup:project`). Combined view shows both.
            gist_dist = working_memory.get("recent_gist_source_distribution") or {}
            window = gist_dist.get("window_size", 0)
            by_origin = gist_dist.get("by_origin") or {}
            untagged = gist_dist.get("untagged", 0)
            if window > 0:
                parts = [f"{o}: {n}" for o, n in
                         sorted(by_origin.items(), key=lambda x: (-x[1], x[0]))]
                if untagged:
                    parts.append(f"untagged: {untagged}")
                lines.append(
                    f"  - Recent gists (last {window}) by origin: "
                    f"{', '.join(parts)}. If one origin dominates while "
                    f"other sources sit in the ingest queue, your themes "
                    f"are fitted to a biased slice — read [high] confidence "
                    f"tags as 'high given what I've seen', not 'high full stop'."
                )
            # Slice 9.2.1 (2026-05-16): sibling dispatch posture
            # (A-only). Per overseer's explicit ask: numbers only,
            # no nudges, no "suggested next action" field — the
            # recommendation is the overseer's job and they want
            # the friction of deciding. No Category B/C fields here
            # by design; we want the overseer to feel the absence
            # of B telemetry before specifying what it should look
            # like. See memory/agent_ecosystem_design.md.
            sib_today = working_memory.get("sibling_dispatched_today")
            sib_cap = working_memory.get("sibling_daily_cap")
            sib_unrated = working_memory.get("sibling_unrated_count")
            sib_pending = working_memory.get("sibling_pending_for_me")
            if sib_today is not None and sib_cap is not None:
                lines.append(
                    f"  - Sibling (Cat A) dispatches today: "
                    f"{sib_today}/{sib_cap}. "
                    f"Unrated completed (you owe a rating): "
                    f"{sib_unrated or 0}. "
                    f"In flight (dispatched, awaiting result): "
                    f"{sib_pending or 0}."
                )
            # Slice 9.6 CP3 (2026-05-19): pending notification responses
            # from Tory — the Bell tab now logs his clicks on custom
            # action buttons and his free-text replies, queued for you
            # to read via get_pending_notification_responses tool.
            # Non-zero = Tory has actually used the channel since last
            # tick; you should fetch and act.
            pending_resp = working_memory.get("pending_notification_responses")
            if pending_resp:
                lines.append(
                    f"  - Notification responses pending your read: "
                    f"**{pending_resp}** (Tory clicked an action button "
                    f"or sent free-text reply on a notification you "
                    f"emitted). Use `get_pending_notification_responses` "
                    f"to fetch + act."
                )
            # Slice 9.4 CP2 (2026-05-16): git ingest channel freshness.
            # Per overseer's explicit caveat: surface BOTH the last-
            # successful-run timestamp AND any repos that got skipped
            # (and why), so the freshness block answers "what am I NOT
            # seeing on the git channel?" not just "when did it last
            # refresh?" — the silent-blindness failure mode is more
            # dangerous than the staleness one.
            git_state = working_memory.get("git_ingest") or {}
            git_last_run = git_state.get("last_run_at")
            git_sum = git_state.get("summary") or {}
            if git_last_run:
                # Age of last run
                from datetime import datetime, timezone
                git_age_minutes = None
                try:
                    g = datetime.fromisoformat(
                        git_last_run.replace("Z", "+00:00"))
                    git_age_minutes = max(
                        0,
                        int((datetime.now(timezone.utc) - g).total_seconds()
                            / 60),
                    )
                except Exception:
                    pass
                if git_age_minutes is not None:
                    if git_age_minutes < 60:
                        age_str = f"{git_age_minutes}m ago"
                    elif git_age_minutes < 24 * 60:
                        age_str = f"{git_age_minutes // 60}h ago"
                    else:
                        age_str = f"{git_age_minutes // (24*60)}d ago"
                else:
                    age_str = git_last_run
                attempted = git_sum.get("repos_attempted") or []
                skipped = git_sum.get("repos_skipped") or []
                # Per-attempt count: just the repo names + insert counts
                if attempted:
                    bits = ", ".join(
                        f"{a['repo'].split('/')[-1]}"
                        f"({a.get('rows_inserted', 0)}+/"
                        f"{a.get('rows_duplicate', 0)}=)"
                        for a in attempted
                    )
                    lines.append(
                        f"  - Git ingest channel: last ran {age_str}; "
                        f"{len(attempted)} repos attempted ({bits})."
                    )
                else:
                    lines.append(
                        f"  - Git ingest channel: last ran {age_str}; "
                        f"0 repos attempted."
                    )
                if skipped:
                    # Always list skips — they are the silent-blindness
                    # signal the overseer specifically wanted exposed.
                    skip_bits = "; ".join(
                        f"{s['repo']}: {s['reason']}" for s in skipped[:5])
                    extra = (f" (+{len(skipped) - 5} more)"
                             if len(skipped) > 5 else "")
                    lines.append(
                        f"    Repos SKIPPED this run: {skip_bits}{extra}"
                    )
            else:
                # Channel never ran — possible reasons: disabled, no PAT,
                # or first-boot before the interval elapsed. Don't speculate;
                # just surface the absence.
                if working_memory.get("git_ingest") is not None:
                    lines.append(
                        "  - Git ingest channel: no successful run yet "
                        "(check loop_git_ingest_enabled, PAT, and the "
                        "loop_git_ingest_repos list)."
                    )
            lines.append("")

        top_projects = working_memory.get("top_projects") or []
        if top_projects:
            lines.append("Top projects (active, recently touched):")
            for p in top_projects[:5]:
                lines.append("  - {tag} ({touched}): {name}".format(
                    tag=p.get("tag", "?"),
                    touched=(p.get("last_touched") or "")[:10],
                    name=_trunc(p.get("name") or "", 80),
                ))
        todos = working_memory.get("open_todos") or []
        if todos:
            lines.append("Open reminders ({}):".format(len(todos)))
            for t in todos[:5]:
                lines.append("  - " + _trunc(
                    (t.get("content") or "").replace("\n", " "), 160))
        digest = working_memory.get("last_week_digest") or ""
        if digest:
            lines.append("Last week digest:")
            lines.append("  " + _trunc(digest, 800))
        lines.append("")
    else:
        lines.append("## Working memory")
        lines.append("(not yet built — first tick may not have run)")
        lines.append("")

    # ── Recent gists ─────────────────────────────────────────
    if recent_gists:
        lines.append("## Recent gists ({})".format(len(recent_gists)))
        for g in recent_gists[:5]:
            label = g.get("period_label") or ""
            lines.append("  - [{c}] {label}: {body}".format(
                c=g.get("confidence", "med"),
                label=label[:30],
                body=_trunc(g.get("body", ""), 200),
            ))
        lines.append("")

    # ── Active questions WITH evidence (primary axis post-3f.5) ──
    # Canonical place for chat to find questions + their threads.
    if active_questions:
        lines.append("## Open questions with their evidence ({})".format(
            len(active_questions)))
        for q in active_questions[:5]:
            lc = q.get("lifecycle", "active")
            ec = q.get("evidence_count", 0)
            lines.append("  - [{c} · {lc} · {n} evidence] {q}".format(
                c=q.get("confidence", "med"), lc=lc, n=ec,
                q=q.get("question", ""),
            ))
            for ev in (q.get("recent_evidence") or [])[:2]:
                contrib = ev.get("contribution", "supports")
                body = (ev.get("evidence_body")
                        or ev.get("reason") or "")[:160]
                lines.append("      • [{}] {}".format(contrib, body))
        lines.append("")

    # ── Themes ───────────────────────────────────────────────
    if recent_themes:
        lines.append("## Themes ({})".format(len(recent_themes)))
        for t in recent_themes[:5]:
            lines.append("  - [{c}] {title}".format(
                c=t.get("confidence", "med"),
                title=t.get("title", "")))
        lines.append("")

    # ── Recent automation rollups (cap to 2 — chatty + rarely the
    # thing the user asks about). Anomaly rows always pass.
    if recent_rollups:
        anomaly_rows = [r for r in recent_rollups if r.get("error_signals", 0)]
        regular_rows = [r for r in recent_rollups if not r.get("error_signals", 0)]
        keep = anomaly_rows[:2] + regular_rows[: max(0, 2 - len(anomaly_rows[:2]))]
        if keep:
            lines.append("## Recent automation rollups ({})".format(len(keep)))
            for r in keep:
                anomaly = " ANOMALY" if r.get("error_signals", 0) else ""
                lines.append(
                    "  - {date} {project}: {n} runs, {sum}{anom}".format(
                        date=r.get("rollup_date", ""),
                        project=r.get("project", ""),
                        n=r.get("session_count", 0),
                        sum=_trunc(r.get("summary", ""), 120),
                        anom=anomaly,
                    )
                )
            lines.append("")

    # ── Future overseer notes (institutional) ────────────────
    # Compass, not full text. Most recent 1 × 400 chars (was 2 × 500
    # in dev.9). If a specific note matters in detail, the user can
    # drill via n:N.
    if future_notes:
        lines.append(
            "## Notes for future overseer (institutional memory)")
        for n in future_notes[-1:]:
            lines.append("  --- by {} at {} ---".format(
                n.get("instance_id", "?"),
                (n.get("written_at") or "")[:19],
            ))
            lines.append(_trunc(n.get("body", ""), 400))
        lines.append("")

    # ── Overseer journal (your own thinking across time) ─────
    # Reading your own prior reflections is per locked design
    # (3f.5/#1) — friction between past and present reading develops
    # perspective. Trimmed to 3 × 250 (was 4 × 350 in dev.9). Older
    # entries can be drilled via j:N if a specific thread matters.
    if recent_journal:
        lines.append("## Your recent journal entries (read for thread, not for facts)")
        for j in recent_journal[-3:]:
            lines.append("  --- {} prov={} ---".format(
                (j.get("written_at") or "")[:19],
                j.get("provisionality", "med"),
            ))
            lines.append(_trunc(j.get("body", ""), 250))
        lines.append("")

    # ── Human journal entries (the user's textarea) ──────────────
    # Slice 10: previously the chat handler only loaded
    # `overseer_journal` (your own tick reflections) and missed
    # `human_journal_entries` entirely — so the user could write a
    # journal entry, ask "did you see what I wrote", and get a
    # confidently wrong answer about its own past entry. Now we
    # always include the user's most recent textarea entries inline
    # alongside the deeper-dive `get_recent_human_journal` tool.
    if recent_human_journal:
        lines.append("## User's recent journal entries (their own writing — read carefully)")
        for h in recent_human_journal[:5]:
            ts = (h.get("created_at") or "")[:19]
            etype = h.get("entry_type", "free")
            lines.append("  --- {} type={} ---".format(ts, etype))
            lines.append(_trunc(h.get("text", ""), 500))
        lines.append("")

    return "\n".join(lines)


def build_blindspots_block(*, db, model: str,
                           topic: str = "") -> str:
    """Per locked design (3f.5/#4): the meta-honesty layer. Surface
    blindspots that apply to (model, topic) so the chat overseer
    answers WITH self-awareness about its own failure modes.
    Pulled into the system prompt as its own section."""
    try:
        bs = applicable_blindspots(
            db=db, model=model, topic=topic,
            record_application=True,
        )
    except Exception as e:
        log.warning("blindspots lookup failed: %s", e)
        return ""
    return format_caveat_block(bs)


def assemble_messages(*, persona: str, context_block: str,
                      history: list[dict],
                      max_history_turns: int = 20) -> list[dict]:
    """Build the OAI-format messages list:
    [system: persona + context], history (user/assistant turns), ...

    History is the chronological tail of chat_messages. We take the
    most recent max_history_turns turns to keep token cost bounded.
    The newest user message is assumed to already be the last item in
    history.
    """
    # Use a single combined system message
    sys_content = persona + "\n\n" + context_block
    msgs: list[dict] = [{"role": "system", "content": sys_content}]

    # Take tail of history
    tail = history[-max_history_turns:] if max_history_turns > 0 else history
    for h in tail:
        role = h.get("role")
        content = h.get("content") or ""
        if role not in ("user", "assistant"):
            continue
        if not content.strip():
            continue
        msgs.append({"role": role, "content": content})
    return msgs


def respond_to_message(*, db, llm, core_memory, user_message: str,
                       backend: str | None = None,
                       max_tokens: int = 800,
                       temperature: float = 0.7,
                       max_history_turns: int = 20,
                       insight_snippet_enabled: bool = True,
                       attachments: list[dict] | None = None,
                       uploads_dir: str | None = None,
                       sibling_daily_cap: int = 20,
                       voice_mode: bool = False) -> dict:
    """End-to-end: append user msg to chat_messages, build prompt,
    call LLM, persist assistant response, return result dict.

    When insight_snippet_enabled (default True), the system prompt asks
    the LLM to optionally mark insight candidates in its reply with a
    fenced ```insight {...}``` block. Such blocks are stripped from
    the user-visible reply BEFORE persistence and queued in
    pending_interpretations for the user to confirm/reject in the Hub
    Insights tab.

    attachments (Slice 8): list of {filename, mime_type, size, pi_path,
    file_id, sha256} refs. Each must already exist on disk under
    uploads_dir (the Hub uploaded them via /files/uploads first).
    Text/pdf contents get inlined into the user message before the LLM
    call; images become multimodal content blocks. Each attachment is
    persisted as a chat_message_files row keyed to the user turn so
    the chat history can re-render them after a reload.

    Returns:
        {ok, reply, model, backend, latency_ms, cost_usd, history_used,
         user_message_id, assistant_message_id, insight_candidates,
         attachments}
    """
    if not user_message or not user_message.strip():
        # Slice 8: an empty message + only attachments is valid (e.g.
        # "here, look at this screenshot"). Substitute a minimal stub
        # so the persisted chat_messages row isn't blank.
        if attachments:
            user_message = "(see attached file{})".format(
                "s" if len(attachments) > 1 else "")
        else:
            return {"ok": False, "error": "empty message"}

    # 1. Persist the user turn
    # 1.0 BEFORE persisting: if this user message reads as a correction
    # of the immediately-prior assistant turn, log a correction. The
    # prior assistant message is the most recent role='assistant' row
    # in chat_messages (we haven't appended this user message yet, so
    # the search is correct). 3i CP2.
    chat_correction_id = None
    try:
        recent_for_correction = db.recent_chat_messages(limit=8)
        prior_asst = next(
            (m for m in reversed(recent_for_correction)
             if m.get("role") == "assistant"),
            None,
        )
        chat_correction_id = maybe_log_chat_correction(
            db=db, user_message=user_message,
            assistant_message_row=prior_asst,
        )
    except Exception as e:
        logging.getLogger("plugin.overseer.chat").exception(
            "chat correction detection failed: %s", e,
        )

    user_id = db.append_chat_message(role="user", content=user_message)

    # 1.5: Slice 8 — read attachments off disk and persist refs FK'd to
    # the user turn we just created. Records are read independently of
    # any frontend hint about file kind (defense in depth) and paths
    # are sandboxed under uploads_dir.
    attachment_records, text_inlines, image_blocks = load_attachments(
        attachments, uploads_dir)
    persisted_attachments: list[dict] = []
    for rec in attachment_records:
        try:
            file_row_id = db.append_chat_file(
                chat_message_id=user_id,
                filename=rec["filename"],
                mime_type=rec["mime_type"],
                size_bytes=rec["size_bytes"],
                kind=rec["kind"],
                pi_path=rec["pi_path"],
                file_id=rec["file_id"],
                sha256=rec["sha256"],
            )
            persisted_attachments.append({
                "id": file_row_id,
                "chat_message_id": user_id,
                **{k: rec[k] for k in (
                    "filename", "mime_type", "size_bytes",
                    "kind", "pi_path", "file_id", "sha256")},
            })
        except Exception as e:
            log.warning("failed to persist chat_message_file for %s: %s",
                        rec.get("filename"), e)

    # 2. Gather context
    wm_json = db.get_overseer_state("working_memory_json")
    working_memory = None
    if wm_json:
        try:
            working_memory = json.loads(wm_json)
        except Exception:
            working_memory = None
    recent_gists = db.recent_gists(limit=12)
    recent_themes = db.recent_themes(limit=8)
    # Slice 3f.5 #2: questions with their recent evidence — questions
    # are the primary organizing axis; chat overseer should cite
    # specific evidence when discussing them.
    active_questions = db.top_questions_with_evidence(
        limit=10, recent_n=3)
    recent_rollups = db.list_rollups(limit=8)
    future_notes = db.all_future_notes()
    recent_journal = db.recent_journal_entries(limit=8)
    # Slice 10: also load the user's human journal entries so the
    # overseer can see what *they* wrote (not just its own tick
    # reflections). Static context floor; deeper queries go through
    # `chat_tools.get_recent_human_journal`.
    try:
        recent_human_journal = db.list_human_journal_entries(limit=5)
    except Exception as _e:
        log.warning("could not load human_journal_entries: %s", _e)
        recent_human_journal = []
    chat_count_so_far = db.chat_message_count() - 1  # excluding the just-added
    core_stats = core_memory.get_stats() if core_memory else {}

    context_block = build_context_block(
        working_memory=working_memory,
        recent_gists=recent_gists,
        recent_themes=recent_themes,
        active_questions=active_questions,
        recent_rollups=recent_rollups,
        future_notes=future_notes,
        recent_journal=recent_journal,
        recent_human_journal=recent_human_journal,
        chat_message_count=chat_count_so_far,
        core_stats=core_stats,
    )

    # 3. Build history including the just-added user turn
    history = db.recent_chat_messages(limit=max_history_turns + 4)
    messages = assemble_messages(
        persona=OVERSEER_PERSONA,
        context_block=context_block,
        history=history,
        max_history_turns=max_history_turns,
    )

    # 3.5: Append blindspots block — meta-honesty caveats relevant to
    # the chat model + the user message topic.
    chat_model = "anthropic/claude-opus-4.7"  # current chat default
    blindspots_block = build_blindspots_block(
        db=db, model=chat_model, topic=user_message,
    )

    # 4. Call LLM. LLMRouter.complete is (prompt, system) — no messages
    # array — so prior conversation has to live in the system block as
    # a transcript. Two new caps in dev.10:
    #   - keep at most 12 turns (was implicitly ~22 via max_history_turns)
    #   - truncate each message to TRANSCRIPT_PER_MSG chars; long
    #     monologues from earlier in the chat were the dominant bloat
    #     source (~10K chars when both sides got chatty).
    # The latest user message still goes as the `prompt` argument
    # untruncated.
    TRANSCRIPT_MAX_TURNS = 12
    TRANSCRIPT_PER_MSG = 800

    sys_text = messages[0]["content"]
    history_summary = ""
    if len(messages) > 2:
        prior = messages[1:-1]
        if len(prior) > TRANSCRIPT_MAX_TURNS:
            prior = prior[-TRANSCRIPT_MAX_TURNS:]
        if prior:
            history_summary = "\n\n## Recent conversation\n" + "\n".join(
                "{}: {}".format(m["role"].upper(),
                                _trunc(m["content"], TRANSCRIPT_PER_MSG))
                for m in prior
            )
    full_system = sys_text + history_summary
    if blindspots_block:
        full_system = full_system + "\n\n" + blindspots_block
    if insight_snippet_enabled:
        full_system = full_system + "\n\n" + CHAT_INSIGHT_MARKER_INSTRUCTION

    # Slice 8: append inlined text/pdf attachments to the user prompt.
    # Image attachments go through the multimodal `images` channel
    # rather than being inlined as text. Note we don't persist the
    # inlined version to chat_messages.content — that stores what the
    # user actually typed; the file bodies live in chat_message_files
    # for history reload. The model only sees the inlined contents on
    # the live turn.
    effective_user_message = user_message
    if text_inlines:
        effective_user_message = user_message + "".join(text_inlines)

    # ── Slice 10: tool-use loop ─────────────────────────────────
    # Build the messages list directly (proper history, not baked
    # into system as a transcript) and pass tools so the overseer
    # can fetch fresh data on demand. The loop dispatches each
    # tool_call → tool_result message, then re-invokes the LLM
    # until it produces a final reply (finish_reason='stop') or we
    # hit MAX_TOOL_ITER. Every iteration's cost is summed into the
    # persisted assistant message.
    tool_messages: list[dict] = []
    # Replay prior chat history (capped). assemble_messages built a
    # system+history+latest list; we reuse messages[1:-1] for prior
    # turns and craft the latest user message ourselves so we can
    # attach images + inlined text properly.
    if len(messages) > 2:
        prior = messages[1:-1]
        if len(prior) > TRANSCRIPT_MAX_TURNS:
            prior = prior[-TRANSCRIPT_MAX_TURNS:]
        for m in prior:
            tool_messages.append({
                "role": m["role"],
                "content": _trunc(m["content"], TRANSCRIPT_PER_MSG)
                if isinstance(m.get("content"), str)
                else m["content"],
            })
    # Latest user turn (with images if any).
    if image_blocks:
        parts: list[dict] = []
        if effective_user_message:
            parts.append({"type": "text",
                          "text": effective_user_message})
        for img in image_blocks:
            mime = (img.get("mime_type") or "image/png").strip()
            b64 = img.get("data_base64") or ""
            if not b64:
                continue
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": "data:{};base64,{}".format(mime, b64),
                },
            })
        tool_messages.append({"role": "user", "content": parts})
    else:
        tool_messages.append({
            "role": "user", "content": effective_user_message,
        })

    # Strip the transcript-baked-in piece from full_system since
    # we're now sending real history. Keep persona + context + blindspots.
    base_system = sys_text
    if blindspots_block:
        base_system = base_system + "\n\n" + blindspots_block
    if insight_snippet_enabled:
        base_system = base_system + "\n\n" + CHAT_INSIGHT_MARKER_INSTRUCTION
    # Slice 14: voice-mode succinctness directive — appended LAST so
    # it's the freshest instruction in the system block.
    if voice_mode:
        base_system = base_system + "\n\n" + VOICE_MODE_DIRECTIVE

    t0 = time.monotonic()
    total_cost = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    tool_call_audit: list[dict] = []
    last_result: dict = {}

    for iter_num in range(chat_tools.MAX_TOOL_ITER + 1):
        last_result = llm.complete_messages(
            tool_messages,
            system=base_system,
            backend=backend,
            max_tokens=max_tokens,
            temperature=temperature,
            purpose="overseer-chat",
            tools=chat_tools.TOOL_DEFINITIONS,
        )
        if not last_result.get("ok"):
            break
        total_cost += last_result.get("cost_usd", 0.0) or 0.0
        total_prompt_tokens += last_result.get("prompt_tokens", 0) or 0
        total_completion_tokens += last_result.get("completion_tokens", 0) or 0

        tool_calls = last_result.get("tool_calls") or []
        if not tool_calls:
            # Final reply — no more tool calls requested.
            break
        if iter_num >= chat_tools.MAX_TOOL_ITER:
            log.warning("max tool iterations reached, returning last text")
            break

        # Append the assistant's tool-call message so the next call
        # has full context.
        asst_msg = last_result.get("message") or {}
        tool_messages.append({
            "role": "assistant",
            "content": asst_msg.get("content"),
            "tool_calls": tool_calls,
        })
        # Dispatch each tool call, append a tool-role message per result.
        for tc in tool_calls:
            tc_id = tc.get("id") or ""
            fn = tc.get("function") or {}
            fn_name = fn.get("name") or ""
            try:
                fn_args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                fn_args = {}
            log.info("tool: %s(%s)", fn_name, fn_args)
            tool_result = chat_tools.dispatch_tool(
                fn_name, fn_args, db=db, core_memory=core_memory,
                sibling_daily_cap=sibling_daily_cap,
                llm=llm,  # Slice 9.5 CP3: compress_chat needs LLMRouter
            )
            tool_call_audit.append({
                "iter": iter_num,
                "name": fn_name,
                "args": fn_args,
                "result_chars": len(tool_result),
            })
            tool_messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result,
            })
        # Loop continues — model gets another turn with results.

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    result = last_result  # name kept for downstream code

    if not result.get("ok"):
        # Persist a system error note instead of an empty assistant reply
        db.append_chat_message(
            role="assistant",
            content="(error: {})".format(result.get("error", "unknown")),
            backend=result.get("backend", ""),
            model=result.get("model", ""),
            latency_ms=elapsed_ms,
            cost_usd=0.0,
            metadata={"ok": False,
                      "error": result.get("error", "")[:500],
                      "tool_calls": tool_call_audit},
        )
        return {"ok": False,
                "error": result.get("error", "unknown"),
                "user_message_id": user_id,
                "latency_ms": elapsed_ms,
                "attachments": persisted_attachments,
                "tool_calls": tool_call_audit}

    raw_reply = (result.get("text") or "").strip()
    # Persist FIRST so we have an assistant_message_id to attach to
    # any extracted insight candidates. Use the raw (un-stripped) text
    # for persistence so the chat log retains the markers as audit.
    # Slice 10: cost + tokens are SUMMED across the tool-use loop, not
    # just the last call, so the chat_messages row reflects total spend
    # for this exchange.
    asst_id = db.append_chat_message(
        role="assistant", content=raw_reply,
        backend=result.get("backend", ""),
        model=result.get("model", ""),
        latency_ms=elapsed_ms,
        cost_usd=total_cost,
        prompt_tokens=total_prompt_tokens,
        response_tokens=total_completion_tokens,
        metadata={"context_chars": len(base_system),
                  "history_turns_used": max(0, len(messages) - 2),
                  "tool_calls": tool_call_audit,
                  "tool_iterations": len(tool_call_audit)},
    )

    # Now strip insight markers and queue candidates. The user-visible
    # reply is the cleaned version (markers removed); pending_
    # interpretations gets the structured candidates pointing back at
    # this chat message via source_chat_message_id.
    insight_candidates = []
    reply_for_user = raw_reply
    if insight_snippet_enabled:
        try:
            reply_for_user, insight_candidates = (
                extract_and_queue_chat_insights(
                    db=db,
                    reply_text=raw_reply,
                    chat_message_id=asst_id,
                )
            )
        except Exception as e:
            logging.getLogger("plugin.overseer.chat").exception(
                "chat insight extraction failed: %s", e,
            )

    return {
        "ok": True,
        "reply": reply_for_user,
        "model": result.get("model"),
        "backend": result.get("backend"),
        "latency_ms": elapsed_ms,
        "cost_usd": total_cost,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "user_message_id": user_id,
        "assistant_message_id": asst_id,
        "history_turns_used": max(0, len(messages) - 2),
        "tool_calls": tool_call_audit,
        "tool_iterations": len(tool_call_audit),
        "insight_candidates": insight_candidates,
        "chat_correction_id": chat_correction_id,   # 3i CP2
        "attachments": persisted_attachments,        # Slice 8
    }


# ── Slice 9.5 CP3 (2026-05-19): chat history compression ───────────
#
# Folds older chat turns into a single synthesized "system" message
# summarizing topics, decisions, and tool-call audit. Two trigger
# surfaces:
#   1. Manual: POST /plugins/overseer/chat/compress (slash command)
#   2. Tool:   overseer can call compress_chat mid-conversation
#              when it notices its own context bloating
#
# Strategy: keep the N most recent raw messages, summarize everything
# older into ONE message, DELETE the originals, INSERT the synthetic
# at a created_at equal to the oldest dropped message so it sorts
# correctly into chronological position at the head of the thread.
#
# Cost target: < $0.02 per compression. Sonnet on ~12K input tokens
# of older history produces ~600 tokens of summary. Cheap relative
# to the ongoing Opus chat cost the compression bounds.

COMPRESS_KEEP_RECENT_DEFAULT = 12
COMPRESS_PROMPT = """You are condensing the older portion of a chat thread \
between a user (Tory) and an AI agent (the overseer) so the recent \
conversation has continuity without paying for the full history every \
turn.

Below is the thread to compress, newest at the bottom. Produce a tight, \
bullet-structured summary covering:

1. Main topics discussed — what was the conversation actually about?
2. Decisions Tory made, directives he gave, or commitments either party made.
3. Tools the overseer called (preserve tool names + key results that \
   informed downstream reasoning).
4. Open threads — anything pending, undelivered, or explicitly deferred.
5. Anything Tory expressed strong feeling about (positive or negative) \
   that the future-overseer should know.

Constraints:
- Under 800 words.
- No preamble. No "Summary:" header. Start directly with the bullets.
- Use Markdown headings + bullets for structure.
- Preserve specific names (people, projects, files, slice IDs, commit \
  SHAs) verbatim — those are load-bearing for memory recall.
- If a topic was raised but went nowhere, say "Raised but not resolved: ..." \
  rather than dropping it silently.

Thread to compress:
"""


def compress_chat_history(*, db, llm, keep_recent: int = COMPRESS_KEEP_RECENT_DEFAULT
                          ) -> dict:
    """Fold older chat messages into one synthesized prefix message.

    Returns {"ok": bool, "messages_before": int, "messages_after": int,
             "compressed_summary": str, "cost_usd": float,
             "compressed_message_id": int, "error": str}.

    No-ops cleanly:
      - If total chat messages <= keep_recent + 1, returns ok=True with
        messages_before == messages_after.
      - If keep_recent < 2, clamps to 2 to keep at least one Q/A pair
        as raw context.
    """
    keep_recent = max(2, int(keep_recent))
    total = db.chat_message_count()
    if total <= keep_recent + 1:
        # Nothing meaningful to compress
        return {
            "ok": True, "messages_before": total, "messages_after": total,
            "compressed_summary": "",
            "cost_usd": 0.0,
            "compressed_message_id": 0,
            "error": "",
        }

    # Pull all messages oldest-first. recent_chat_messages returns
    # chronological order despite the SELECT being DESC internally.
    all_msgs = db.recent_chat_messages(limit=max(total, 1000))
    if len(all_msgs) <= keep_recent + 1:
        return {
            "ok": True, "messages_before": len(all_msgs),
            "messages_after": len(all_msgs),
            "compressed_summary": "", "cost_usd": 0.0,
            "compressed_message_id": 0, "error": "",
        }

    to_compress = all_msgs[:-keep_recent]
    to_keep = all_msgs[-keep_recent:]
    if not to_compress:
        return {
            "ok": True, "messages_before": len(all_msgs),
            "messages_after": len(all_msgs),
            "compressed_summary": "", "cost_usd": 0.0,
            "compressed_message_id": 0, "error": "",
        }

    # Build the thread transcript for the summarizer. Truncate each
    # message at 1200 chars so a giant assistant monologue doesn't
    # dominate the prompt budget — the summary will lose granular
    # detail anyway. Tool-call audit lives in metadata_json; surface
    # the tool names so the summary can mention them.
    parts = []
    for m in to_compress:
        role = (m.get("role") or "?").upper()
        ts = m.get("local_created_at") or m.get("created_at") or "?"
        content = (m.get("content") or "").strip()
        if len(content) > 1200:
            content = content[:1200] + " […]"
        # Surface tool calls if any
        tool_calls_summary = ""
        try:
            meta = json.loads(m.get("metadata_json") or "{}")
            tcs = meta.get("tool_calls") or []
            if tcs:
                names = [tc.get("name") or "?" for tc in tcs]
                tool_calls_summary = f"\n[tool calls: {', '.join(names)}]"
        except Exception:
            pass
        parts.append(f"[{ts}] {role}:\n{content}{tool_calls_summary}")
    transcript = "\n\n".join(parts)

    prompt = COMPRESS_PROMPT + transcript

    # Use Sonnet for compression — cheaper than Opus, fully capable
    # for summarization. Bypass chat tools (compression doesn't need
    # to call other tools).
    log.info(
        "compress_chat_history: compressing %d msgs, keeping %d recent",
        len(to_compress), len(to_keep),
    )
    result = llm.complete(
        prompt,
        system="You compress chat threads for downstream context. "
               "Bullet structure. Under 800 words. Preserve specific "
               "names and tool calls verbatim.",
        backend="openrouter",
        model="anthropic/claude-sonnet-4.6",
        max_tokens=1200,
        temperature=0.3,
        purpose="chat-compress",
    )
    if not result.get("ok"):
        return {
            "ok": False, "messages_before": len(all_msgs),
            "messages_after": len(all_msgs),
            "compressed_summary": "", "cost_usd": 0.0,
            "compressed_message_id": 0,
            "error": result.get("error", "LLM failed"),
        }
    summary_text = (result.get("text") or "").strip()
    cost = float(result.get("cost_usd") or 0.0)

    # Wrap the summary so it's clearly identifiable as compressed
    # context, not a real user/assistant turn.
    wrapped = (
        f"**[Compressed context — {len(to_compress)} earlier messages "
        f"folded into this summary at "
        f"{(to_compress[0].get('local_created_at') or to_compress[0].get('created_at') or '?')} "
        f"→ {(to_compress[-1].get('local_created_at') or to_compress[-1].get('created_at') or '?')}]**\n\n"
        + summary_text
    )

    # Delete the old messages (and their chat_message_files via
    # explicit cleanup — FK CASCADE is off per overseer_db.py:
    # clear_chat()'s pattern). Then insert the synthetic at the
    # oldest dropped timestamp so it sorts to the head.
    ids_to_delete = [int(m["id"]) for m in to_compress if m.get("id")]
    oldest_ts = to_compress[0].get("created_at")
    try:
        new_id = db.compress_chat_replace(
            old_ids=ids_to_delete,
            summary_content=wrapped,
            created_at=oldest_ts,
            metadata={
                "compressed_from_count": len(to_compress),
                "compressed_from_ids": ids_to_delete,
                "compress_cost_usd": cost,
                "compress_model": result.get("model", ""),
            },
        )
    except Exception as e:
        log.exception("compress_chat_history: DB write failed: %s", e)
        return {
            "ok": False, "messages_before": len(all_msgs),
            "messages_after": len(all_msgs),
            "compressed_summary": "", "cost_usd": cost,
            "compressed_message_id": 0,
            "error": f"DB write failed: {e}",
        }

    return {
        "ok": True,
        "messages_before": len(all_msgs),
        "messages_after": len(to_keep) + 1,  # +1 for the synthetic prefix
        "compressed_summary": summary_text,
        "cost_usd": cost,
        "compressed_message_id": new_id,
        "error": "",
    }
