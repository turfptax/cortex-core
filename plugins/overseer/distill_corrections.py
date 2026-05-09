"""Slice 3i CP2: distill uncondidated corrections → blindspot proposals.

A correction is something the user told the overseer "you got this
wrong." Three sources today:
  - chat: heuristic detector flags user messages that read as a
    correction of the previous assistant turn
  - dialectic-resolution: when the user picks opus over gemma
    (or vice versa, or proposes a third), the losing model gets a
    correction logged
  - manual: external API call

Corrections accumulate in interpretation_corrections with
used_in_blindspot_id NULL until they're "distilled" — a Sonnet pass
clusters related corrections into proposed BLINDSPOTS that land in
pending_interpretations (kind='blindspot') for the user to confirm.

A blindspot is a generalization across multiple corrections, not a
1:1 transcription. So distillation is genuinely interpretive — Sonnet
is the right model for it.

Single LLM call per distill run, bounded by max_cost_usd_per_distill
(default $0.05). Daily budget caps the whole thing on top.
"""

import json
import logging
import re
from datetime import datetime, timezone


log = logging.getLogger("plugin.overseer.distill_corrections")


DISTILL_PROMPT_TEMPLATE = """You are reviewing recent CORRECTIONS — moments where the user told the overseer (or one of its models) that an interpretation was wrong.

Your job: cluster these corrections into BLINDSPOT PROPOSALS — generalizations like "Opus over-hedges on identity questions" — that capture WHAT the model gets systematically wrong, not just the specific correction.

A blindspot has structure:
  - model_pattern: glob like "*opus*", "*gemma*", "*sonnet*", or "*" for cross-model
  - topic_pattern: substring (case-insensitive); empty = applies to any topic
  - direction: one of "downgrades" | "overstates" | "misses" | "hedges" | "general"
  - confidence_adjustment: -1 (treat reported confidence as too low),
                            0 (don't adjust),
                            +1 (treat reported confidence as too high)
  - body: 1-3 sentences naming the blindspot
  - rationale: what evidence justifies it (cite correction ids: c:42, c:51)

EXISTING blindspots (do NOT re-propose, ok to refine cautiously):
{existing_blindspots}

CORRECTIONS to distill (newest first, c:<id>):
{corrections_block}

Cluster aggressively but conservatively:
- 2+ corrections sharing topic+model → STRONG candidate for a blindspot
- 1 correction → only propose if the topic is unmistakable (skip otherwise)
- A correction may belong to MULTIPLE candidates — split appropriately
- Do NOT propose if the existing blindspot list already covers it

Output a single JSON object on one line:

{{"blindspots": [
  {{"title": "<noun phrase, <80 chars — e.g. 'Opus hedges on identity questions'>",
    "model_pattern": "*opus*",
    "topic_pattern": "identity|consciousness",
    "direction": "hedges",
    "confidence_adjustment": -1,
    "body": "<1-3 sentence statement of the blindspot>",
    "rationale": "<why; cite c:42, c:51>",
    "confidence": "med" or "high",
    "supporting_correction_ids": [42, 51]}}
]}}

If nothing crystallizes: {{"blindspots": []}}.
"""


def _format_existing_blindspots(rows, *, limit=12):
    if not rows:
        return "  (none)"
    out = []
    for r in rows[:limit]:
        body = (r.get("body") or "").strip().split("\n")[0]
        if len(body) > 100:
            body = body[:100] + "…"
        out.append("  - {} | {} → {}".format(
            r.get("model_pattern") or "*",
            r.get("topic_pattern") or "(any)",
            body,
        ))
    return "\n".join(out)


def _format_corrections(rows, *, max_chars=8000):
    if not rows:
        return "  (none)"
    lines = []
    used = 0
    for r in rows:
        cid = r.get("id")
        model = (r.get("model") or "?").split("/")[-1]
        topic = (r.get("topic") or "").strip().replace("\n", " ")[:80]
        wrong = (r.get("what_was_wrong") or "").strip().replace("\n", " ")
        if len(wrong) > 200:
            wrong = wrong[:200] + "…"
        corr = (r.get("user_correction") or "").strip().replace("\n", " ")
        if len(corr) > 300:
            corr = corr[:300] + "…"
        src = r.get("source") or "?"
        line = "  c:{} [{}/{}] topic={!r}\n      WRONG: {}\n      USER:  {}".format(
            cid, src, model, topic, wrong, corr,
        )
        if used + len(line) > max_chars:
            lines.append("  …(truncated, {} more corrections not shown)".format(
                len(rows) - len(lines)))
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


_JSON_OBJECT_RX = re.compile(r"\{[\s\S]*\}", re.MULTILINE)
_VALID_DIRECTIONS = ("downgrades", "overstates", "misses", "hedges", "general")


def parse_distill_response(text):
    """Pull {"blindspots": [...]} out of LLM response. Tolerates markdown
    fences + leading/trailing prose. Returns list[dict]; empty on failure."""
    if not text:
        return []
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)
    m = _JSON_OBJECT_RX.search(raw)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        log.warning("distill: bad JSON: %s", m.group(0)[:200])
        return []
    items = obj.get("blindspots")
    if not isinstance(items, list):
        return []
    cleaned = []
    for raw_bs in items:
        if not isinstance(raw_bs, dict):
            continue
        title = (raw_bs.get("title") or "").strip()
        body = (raw_bs.get("body") or "").strip()
        if not title or not body:
            continue
        confidence = (raw_bs.get("confidence") or "med").strip().lower()
        if confidence == "low":
            continue
        if confidence not in ("med", "high"):
            confidence = "med"
        direction = (raw_bs.get("direction") or "general").strip().lower()
        if direction not in _VALID_DIRECTIONS:
            direction = "general"
        try:
            ca = int(raw_bs.get("confidence_adjustment") or 0)
        except (TypeError, ValueError):
            ca = 0
        if ca < -1: ca = -1
        if ca > 1:  ca = 1
        sup = raw_bs.get("supporting_correction_ids") or []
        if not isinstance(sup, list):
            sup = []
        clean_sup = []
        for s in sup:
            try:
                clean_sup.append(int(s))
            except (ValueError, TypeError):
                continue
        cleaned.append({
            "title": title[:200],
            "body": body[:2000],
            "model_pattern": (raw_bs.get("model_pattern") or "*").strip()[:200],
            "topic_pattern": (raw_bs.get("topic_pattern") or "").strip()[:300],
            "direction": direction,
            "confidence_adjustment": ca,
            "rationale": (raw_bs.get("rationale") or "").strip()[:1000],
            "confidence": confidence,
            "supporting_correction_ids": clean_sup,
        })
    return cleaned


def distill_uncondidated_corrections(
    *, db, llm,
    max_corrections=30,
    max_cost_usd=0.05,
    budget=None,
    triggered_by="manual",
):
    """Run a distill pass over recent uncondidated corrections.

    Returns: dict with {ok, corrections_seen, candidates_proposed,
    candidates_deduped, cost_usd, scan_id}.
    Logs the scan in insight_scans regardless of outcome (scan_kind=
    'corrections-distill') so the user can see it in the same Recent
    Scans timeline as gist-arc scans.
    """
    if budget is not None and budget.exhausted():
        return {"ok": False, "error": "tick budget exhausted before distill"}

    corrections = db.list_corrections(
        undistilled_only=True, limit=max_corrections,
    )
    if len(corrections) < 1:
        sid = db.log_insight_scan(
            scan_kind="corrections-distill",
            gists_seen=0, candidates_proposed=0,
            triggered_by=triggered_by, ok=True,
            error="no uncondidated corrections",
        )
        return {
            "ok": True,
            "corrections_seen": 0,
            "candidates_proposed": 0,
            "candidates_deduped": 0,
            "cost_usd": 0.0,
            "scan_id": sid,
            "note": "nothing to distill",
        }

    existing = db.list_blindspots(active_only=True, limit=50)
    prompt = DISTILL_PROMPT_TEMPLATE.format(
        existing_blindspots=_format_existing_blindspots(existing),
        corrections_block=_format_corrections(corrections),
    )

    result = llm.complete(
        prompt,
        purpose="distill-corrections",
        max_tokens=2000,
        temperature=0.3,
    )
    if budget is not None:
        budget.charge(result)
    cost_usd = float(result.get("cost_usd") or 0.0)

    if cost_usd > max_cost_usd:
        log.warning(
            "distill cost %.4f > cap %.4f", cost_usd, max_cost_usd,
        )

    if not result.get("ok"):
        sid = db.log_insight_scan(
            scan_kind="corrections-distill",
            gists_seen=len(corrections),
            candidates_proposed=0,
            cost_usd=cost_usd,
            triggered_by=triggered_by, ok=False,
            error=result.get("error", "llm error")[:500],
        )
        return {
            "ok": False,
            "corrections_seen": len(corrections),
            "candidates_proposed": 0,
            "cost_usd": cost_usd,
            "scan_id": sid,
            "error": result.get("error", "llm error"),
        }

    blindspots = parse_distill_response(result.get("text") or "")

    proposed_n = 0
    deduped_n = 0
    proposed_by = "sonnet:distill-corrections"
    actual_model = result.get("model") or ""
    if actual_model:
        proposed_by = "{}:distill-corrections".format(actual_model)

    for bs in blindspots:
        new_id = db.insert_pending_interpretation(
            kind="blindspot",
            title=bs["title"],
            body=bs["body"],
            confidence=bs["confidence"],
            direction=bs["direction"],
            rationale=bs["rationale"],
            proposed_by=proposed_by,
            source_kind="corrections-distill",
            source_pointer_ids=bs["supporting_correction_ids"],
            bs_model_pattern=bs["model_pattern"],
            bs_topic_pattern=bs["topic_pattern"],
            bs_confidence_adjustment=bs["confidence_adjustment"],
        )
        if new_id is None:
            deduped_n += 1
        else:
            proposed_n += 1

    sid = db.log_insight_scan(
        scan_kind="corrections-distill",
        gists_seen=len(corrections),
        candidates_proposed=proposed_n,
        candidates_deduped=deduped_n,
        cost_usd=cost_usd,
        triggered_by=triggered_by, ok=True,
    )

    return {
        "ok": True,
        "corrections_seen": len(corrections),
        "candidates_proposed": proposed_n,
        "candidates_deduped": deduped_n,
        "cost_usd": cost_usd,
        "scan_id": sid,
    }


# ── Chat-side correction detection (heuristic, no LLM) ────────────


# High-precision triggers — false negatives are fine, false positives are
# annoying. Patterns are anchored to the start of the message so casual
# usage like "no problem" doesn't trip.
_CORRECTION_RX = re.compile(
    r"^\s*("
    r"no,?\s+(that's|that’s|you're|you’re|you've|you’ve|you got|that is|that was)"
    r"|actually,?\s+(no|that's|that’s)"
    r"|that's wrong"
    r"|that’s wrong"
    r"|you'?re wrong"
    r"|let me correct"
    r"|correction[:.]"
    r"|wrong[\.:,]"
    r")",
    re.IGNORECASE,
)


def looks_like_correction(text):
    """Heuristic: does this user message read as a correction of the
    immediately-prior assistant message? Tuned for high precision —
    common false positives (`no problem`, `actually I think we should`)
    don't trigger."""
    if not text:
        return False
    return bool(_CORRECTION_RX.match(text))


def maybe_log_chat_correction(*, db, user_message, assistant_message_row):
    """If the user message reads as a correction of the prior assistant
    message, log a correction row. Returns correction_id or None.

    `assistant_message_row` is a chat_messages row dict (or None — in
    which case nothing is logged because there's no artifact to point
    at)."""
    if not looks_like_correction(user_message):
        return None
    if not assistant_message_row:
        return None
    prior_text = (assistant_message_row.get("content") or "").strip()
    # Topic = first short phrase from the prior assistant text. Cheap
    # but useful for clustering by topic in distillation.
    topic = prior_text.split("\n")[0][:120]
    try:
        return db.log_correction(
            model=assistant_message_row.get("model") or "",
            artifact_table="chat_messages",
            artifact_id=int(assistant_message_row.get("id") or 0) or None,
            topic=topic,
            what_was_wrong=prior_text[:1000],
            user_correction=(user_message or "")[:2000],
            severity="med",
            source="chat",
        )
    except Exception as e:
        log.exception("maybe_log_chat_correction failed: %s", e)
        return None
