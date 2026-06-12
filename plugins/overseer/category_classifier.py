"""Slice 14.7.3 (2026-05-26) — work/cortex/personal classifier.

The rule-based classifier in overseer_db.resolve_category() handles
sessions with cwd signal cleanly. But ~80% of imports are web-AI
conversations (chatgpt, grok-com, grok-twitter) with no cwd. This
module classifies those via a cheap Flash pass over the first user
message + title.

Design:
  - One LLM call per session (no batching — keeps prompts small
    and the response unambiguous).
  - Model: Gemini 2.0 Flash via the existing llm router (purpose
    'category-classify'). Cost ~$0.001/session.
  - Hard cost cap (max_cost_usd) enforced — the runner stops cleanly
    when the cap is hit, doesn't half-classify.
  - Output is one of: 'work' | 'cortex' | 'personal' — never empty,
    never something else. Prompt is strict; if Flash returns junk we
    default to 'personal' (the safest fall-through for casual web AI).
  - The classifier reads each session's .jsonl, extracts the first
    user message (or the title from metadata_json), and ships ~500
    tokens of context to Flash. No need for the full transcript.

Reuses the existing parser (claude_jsonl.parse_claude_code_jsonl) so
the same .jsonl shape that powers summarize-session works here too.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

# These imports mirror loop.py's pattern.
from claude_jsonl import parse_claude_code_jsonl

log_default = logging.getLogger("plugin.overseer.category_classifier")

ALLOWED_CATEGORIES = ("work", "cortex", "personal")
DEFAULT_CATEGORY = "personal"  # safe fall-through for casual chat

SYSTEM_PROMPT = (
    "You classify an AI conversation snippet into one of three "
    "categories for a personal memory system. Respond with EXACTLY "
    "one word — no quotes, no punctuation, no explanation.\n\n"
    "Categories:\n"
    "  work     — clinical / healthcare / an employer / ClientA / "
    "ClientB / ProjectX acquisition / business operations / regulatory / "
    "patient care / medical coding / staff management / financial "
    "decisions about the company\n"
    "  cortex   — Cortex Hub / cortex-core / cortex-desktop / "
    "cortex-pet / cortex-link / cortex-mcp / overseer / wearable "
    "memory system / training pipeline / Pi firmware / the AI "
    "memory project the user is building\n"
    "  personal — Open Muscle / FlexGrid / UAP / UFOSINT / "
    "TruthSea / hardware tinkering / philosophy / casual learning "
    "/ media / general curiosity / fiction / life advice / "
    "everything else not work or cortex\n\n"
    "When unsure, choose 'personal'. Output the single word and "
    "nothing else."
)

MAX_SNIPPET_CHARS = 1500


def _build_snippet(session_row: dict) -> str:
    """Read first user message + title from the session's source file.
    Return a ~1500-char snippet for Flash to classify.

    Falls back to title-from-metadata + project if the .jsonl can't
    be parsed — the title alone is often enough signal.
    """
    parts: list[str] = []

    # Title from metadata if available
    meta = session_row.get("metadata_json") or ""
    if meta:
        try:
            mj = json.loads(meta)
            title = (mj.get("title") or "").strip()
            if title:
                parts.append(f"TITLE: {title}")
        except Exception:
            pass

    # Project field
    proj = (session_row.get("project") or "").strip()
    if proj:
        parts.append(f"PROJECT: {proj}")

    # Source label
    src = (session_row.get("source") or "").strip()
    if src:
        parts.append(f"SOURCE: {src}")

    # First user message from the .jsonl
    src_path = session_row.get("source_path") or ""
    if src_path:
        try:
            p = Path(src_path)
            if p.is_file():
                _meta, messages = parse_claude_code_jsonl(p)
                # First user-role message
                for m in messages:
                    if m.get("role") == "user":
                        body = (m.get("content_text") or "").strip()
                        if body:
                            parts.append(
                                f"FIRST USER TURN: {body[:1200]}")
                        break
        except Exception:
            pass  # title/project alone is often enough

    text = "\n\n".join(parts)
    if len(text) > MAX_SNIPPET_CHARS:
        text = text[:MAX_SNIPPET_CHARS] + " ..."
    return text or "(empty session)"


def _classify_one(*, llm, snippet: str, log) -> tuple[str, dict]:
    """One Flash call. Returns (category, llm_log_dict)."""
    try:
        result = llm.complete(
            snippet,                       # positional prompt
            system=SYSTEM_PROMPT,
            purpose="category-classify",
            max_tokens=8,                  # one word
            temperature=0.0,               # deterministic-ish
        )
    except Exception as e:
        log.warning("category_classify LLM call failed: %s", e)
        return DEFAULT_CATEGORY, {"error": str(e)[:200], "cost_usd": 0}

    if not result.get("ok"):
        return DEFAULT_CATEGORY, {
            "error": result.get("error", "")[:200],
            "cost_usd": float(result.get("cost_usd", 0) or 0),
        }

    text = (result.get("text") or "").strip().lower()
    # Strip common noise
    for c in (".", ",", "'", '"', "`"):
        text = text.replace(c, "")
    # Pick the first allowed token we see — handles "personal." or
    # "category: work" cases.
    chosen = DEFAULT_CATEGORY
    for cat in ALLOWED_CATEGORIES:
        if cat in text:
            chosen = cat
            break
    return chosen, {
        "cost_usd": float(result.get("cost_usd", 0) or 0),
        "raw": text[:80],
    }


def run_batch(*, db, llm, limit: int = 200,
              max_cost_usd: float = 1.0,
              source: str | None = None,
              log=None) -> dict:
    """Classify up to `limit` unclassified sessions, capped at
    `max_cost_usd` total spend. Returns per-category counts +
    cost summary.

    Hard-stops cleanly when either limit or cost is exhausted —
    doesn't leave half-classified rows.
    """
    log = log or log_default
    sessions = db.list_unclassified_sessions(source=source, limit=limit)
    counts: dict = {c: 0 for c in ALLOWED_CATEGORIES}
    counts["default_fallback"] = 0  # parse fails / empty / etc.
    total_cost = 0.0
    n_done = 0
    n_errors = 0
    t0 = time.monotonic()

    for sess in sessions:
        if total_cost >= max_cost_usd:
            log.info(
                "category_classify_batch: cost cap hit ($%.4f / $%.2f) "
                "after %d sessions", total_cost, max_cost_usd, n_done)
            break

        snippet = _build_snippet(sess)
        cat, call_meta = _classify_one(llm=llm, snippet=snippet, log=log)
        if "error" in call_meta:
            n_errors += 1
            cat = DEFAULT_CATEGORY
            counts["default_fallback"] += 1
        else:
            counts[cat] = counts.get(cat, 0) + 1

        try:
            db.set_session_category(
                sess["id"], category=cat,
                set_by="flash-classifier")
        except Exception as e:
            log.warning("set_session_category failed for %s: %s",
                        sess["id"], e)
            n_errors += 1

        total_cost += float(call_meta.get("cost_usd", 0) or 0)
        n_done += 1

    elapsed = time.monotonic() - t0
    return {
        "classified": n_done,
        "errors": n_errors,
        "by_category": counts,
        "cost_usd": round(total_cost, 4),
        "elapsed_s": round(elapsed, 1),
        "queue_remaining": max(0, len(sessions) - n_done),
    }
