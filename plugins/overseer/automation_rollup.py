"""Automation rollup generator — Sonnet 4.6 daily summary per project.

For projects classified as "automation" (e.g. UFOSINT hourly), we don't
summarize each session individually — that's noisy + wasteful. Instead
the loop generates ONE rollup per (project, day) using the cheaper
model. The rollup highlights:
  - count + total/median/max duration
  - error signals (sessions with stack traces, error markers, etc.)
  - outliers (much longer than median)
  - quick semantic gist of what the automation actually did

Output writes a row to automation_rollups + a corresponding gist row in
summaries_gist (so working_memory + the Hub UI surface it like any
other summary).
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from pathlib import Path
from typing import Any

from claude_jsonl import parse_claude_code_jsonl


log = logging.getLogger("plugin.overseer.rollup")


# Crude but useful signals that something went wrong inside a session
_ERROR_RX = re.compile(
    r"(?i)\b(traceback|error[:!]|exception[:!]|"
    r"failed|fatal|stack ?trace|"
    r"\[ERROR\]|ETIMEDOUT|ECONNREFUSED)\b"
)


def _scan_for_error_signals(content: str) -> int:
    """Count loose error-marker hits in a content string."""
    if not content:
        return 0
    return len(_ERROR_RX.findall(content))


def gather_session_facts(import_row: dict, *, max_chars: int = 4000) -> dict:
    """Open the .jsonl referenced by import_row and pull a small set of
    facts: did it look like an error happened, what user prompt kicked
    it off, what was the assistant's last response. Bounded read so
    rolling up 100s of files stays cheap."""
    path = Path(import_row.get("source_path") or "")
    facts = {
        "id": import_row.get("id"),
        "duration_minutes": import_row.get("duration_minutes") or 0,
        "message_count": import_row.get("message_count") or 0,
        "started_at": import_row.get("started_at"),
        "ended_at": import_row.get("ended_at"),
        "first_user_prompt": "",
        "last_assistant_response": "",
        "error_hits": 0,
    }
    if not path.is_file():
        return facts
    try:
        _, messages = parse_claude_code_jsonl(path)
    except Exception as e:
        log.warning("could not parse %s for rollup: %s", path, e)
        return facts

    user_msgs = [m for m in messages if m.get("role") == "user"]
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    if user_msgs:
        facts["first_user_prompt"] = (
            user_msgs[0].get("content_text") or "")[:600]
    if assistant_msgs:
        facts["last_assistant_response"] = (
            assistant_msgs[-1].get("content_text") or "")[:600]

    error_hits = 0
    chars_scanned = 0
    for m in messages:
        chunk = m.get("content_text") or ""
        if not chunk:
            continue
        error_hits += _scan_for_error_signals(chunk)
        chars_scanned += len(chunk)
        if chars_scanned >= max_chars * 4:
            break
    facts["error_hits"] = error_hits
    return facts


def build_rollup_prompt(*, project: str, rollup_date: str,
                        facts: list[dict], stats: dict) -> str:
    """Compose the Sonnet prompt. Keep it small — rollups should be
    cheap. Each session gets one short line."""
    sample_lines = []
    # Show up to 8 outlier-ish sessions: longest, sessions with errors,
    # and a few representative ones.
    indexed = list(enumerate(facts))
    error_first = sorted(
        indexed,
        key=lambda ix: (-(ix[1].get("error_hits") or 0),
                        -(ix[1].get("duration_minutes") or 0)),
    )
    chosen = []
    seen_ids = set()
    for _, f in error_first:
        if f["id"] in seen_ids:
            continue
        chosen.append(f)
        seen_ids.add(f["id"])
        if len(chosen) >= 8:
            break

    for f in chosen:
        ts = (f.get("started_at") or "")[11:19]
        dur = f.get("duration_minutes") or 0
        msgs = f.get("message_count") or 0
        err = f.get("error_hits") or 0
        prompt = (f.get("first_user_prompt") or "")[:160].replace("\n", " ")
        marker = " [ERR]" if err else ""
        sample_lines.append(
            "- {ts} dur={dur}m msgs={msgs}{err} prompt={prompt}".format(
                ts=ts, dur=dur, msgs=msgs, err=marker, prompt=prompt,
            )
        )
    samples = "\n".join(sample_lines) if sample_lines else "(no samples available)"

    return (
        "You are summarizing a day of automated runs for a single project.\n"
        "Give ONE short paragraph (3-5 sentences max) describing:\n"
        "  1. what the automation does (inferred from prompts and timing)\n"
        "  2. anomalies if any (errors, much longer than median, etc.)\n"
        "  3. anything worth a human look\n"
        "If everything looks normal, say so plainly — don't pad.\n\n"
        "Project: {project}\n"
        "Date (UTC): {date}\n"
        "Stats: {n} sessions, total {total_min}m, median {median_min:.1f}m, "
        "max {max_min}m, total {total_msgs} messages, "
        "{err_n} sessions with error markers.\n\n"
        "Sample sessions (highest-priority first):\n{samples}\n\n"
        "Reply with the paragraph only. No preamble. No headings."
    ).format(
        project=project, date=rollup_date,
        n=stats["session_count"],
        total_min=stats["total_minutes"],
        median_min=stats["median_minutes"],
        max_min=stats["max_minutes"],
        total_msgs=stats["total_messages"],
        err_n=stats["error_signals"],
        samples=samples,
    )


def compute_rollup_stats(facts: list[dict]) -> dict:
    durations = [f.get("duration_minutes") or 0 for f in facts]
    msgs = [f.get("message_count") or 0 for f in facts]
    errs = sum(1 for f in facts if (f.get("error_hits") or 0) > 0)
    return {
        "session_count": len(facts),
        "total_minutes": sum(durations),
        "total_messages": sum(msgs),
        "median_minutes": (
            statistics.median(durations) if durations else 0.0),
        "max_minutes": max(durations) if durations else 0,
        "error_signals": errs,
    }


def generate_rollup(*, db, llm, project: str, rollup_date: str,
                    budget=None) -> dict:
    """Build (or re-build) the rollup for one (project, day).

    Reads imports from imported_sessions for that date, gathers per-session
    facts (with error scanning), calls Sonnet 4.6 for a paragraph summary,
    writes the rollup row + a corresponding summaries_gist row.

    Idempotent: re-running for the same (project, date) replaces the row
    and reuses its gist_id if one already exists.

    Returns a result dict with status + counts + the summary text.
    """
    imports = db.imports_for_rollup(project, rollup_date)
    if not imports:
        return {"ok": False, "skipped": "no imports for that date",
                "project": project, "date": rollup_date}

    facts = [gather_session_facts(r) for r in imports]
    stats = compute_rollup_stats(facts)

    prompt = build_rollup_prompt(
        project=project, rollup_date=rollup_date,
        facts=facts, stats=stats)
    result = llm.complete(
        prompt, max_tokens=320, temperature=0.4,
        purpose="auto-tag-notes",  # routes to Sonnet 4.6 via model_overrides
    )
    if budget is not None:
        budget.charge(result)

    if not result.get("ok"):
        return {"ok": False,
                "error": result.get("error", "")[:300],
                "project": project, "date": rollup_date,
                "stats": stats}

    summary_text = (result.get("text") or "").strip().strip('"').strip()
    if not summary_text:
        return {"ok": False, "error": "empty LLM response",
                "project": project, "date": rollup_date}

    # Write gist row first so we can link it from the rollup row.
    existing = db.get_rollup(project, rollup_date)
    gist_text = "[{} rollup] {}".format(project, summary_text)
    gist_id = db.add_gist(
        gist_text,
        period_label="rollup:{}:{}".format(project, rollup_date),
        period_start=rollup_date + " 00:00:00",
        period_end=rollup_date + " 23:59:59",
        confidence="med",
        tags=["auto", "automation-rollup",
              "project:{}".format(project)],
    )

    sample_ids = [r["id"] for r in imports[:20]]
    db.upsert_rollup(
        project=project, rollup_date=rollup_date,
        session_count=stats["session_count"],
        total_messages=stats["total_messages"],
        total_minutes=int(stats["total_minutes"]),
        error_signals=stats["error_signals"],
        median_minutes=stats["median_minutes"],
        max_minutes=stats["max_minutes"],
        summary=summary_text, gist_id=gist_id,
        sample_session_ids=sample_ids,
    )

    return {
        "ok": True, "project": project, "date": rollup_date,
        "summary": summary_text, "gist_id": gist_id,
        "stats": stats,
        "anomaly": stats["error_signals"] > 0,
        "replaced_existing": existing is not None,
    }
