"""Slice 3h: insight generation — propose new theme/pattern/drift
candidates from recent gist arcs.

Conservative on purpose: Sonnet reads a project's recent gists and is
asked to identify ONLY genuinely new things. The result lands in
pending_interpretations for human review (or, in CP2, an auto-confirm
rule). NEVER auto-applied to the live tables.

Single LLM call per scan. Default budget cap: $0.05/scan. Sonnet is
cheap (~$3/$15 per 1M); a typical scan uses well under a cent.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone


log = logging.getLogger("plugin.overseer.insight_scan")


SCAN_PROMPT_TEMPLATE = """You are reviewing the user's gist log for project "{project}".

Time window: {window_start} to {window_end}
Gists in window: {gist_count}

YOUR JOB
Identify NEW interpretive insights that have crystallized in this
window — things the user (or a reviewer) would benefit from having
named. The three categories:

  - THEME: a thematic thread reinforced across multiple gists. Names
    a recurring "what this is about" lens.
  - PATTERN: a recurring behavior, working style, or interaction
    shape (e.g. "starts the day with a spec, then iterates").
  - DRIFT: something that started, stopped, or shifted across the
    window (e.g. "stopped scheduling Sunday work").

WHAT NOT TO DO
- Do NOT re-propose anything already in the EXISTING list below.
- Do NOT propose vague or low-conviction insights. Quality over
  quantity. If nothing crystallizes, return {{"insights": []}}.
- Do NOT propose insights you'd rate "low" confidence. The bar is
  "I'd defend this to a careful reader."
- Do NOT pad with synonyms of existing items.

EXISTING (do not re-propose; ok to refine cautiously):
themes:
{existing_themes}

patterns:
{existing_patterns}

drift:
{existing_drift}

GISTS (chronological, oldest first):
{gist_block}

OUTPUT
Return a single JSON object on one line, no surrounding prose:

{{"insights": [
  {{"kind": "theme",
    "title": "<noun phrase, <80 chars>",
    "body": "<1-3 sentences>",
    "confidence": "med" or "high",
    "rationale": "<why; cite gist ids like g:42, g:51>",
    "supporting_gist_ids": [42, 51]}},
  {{"kind": "drift",
    "title": "<noun phrase>",
    "body": "<1-3 sentences>",
    "confidence": "med" or "high",
    "direction": "started" or "stopped" or "shifted",
    "rationale": "...",
    "supporting_gist_ids": [...]}}
]}}

If nothing genuine: {{"insights": []}}
"""


def _format_existing(rows, *, key="title", limit=12):
    """Render existing themes/patterns/drift as a compact list."""
    if not rows:
        return "  (none)"
    out = []
    for r in rows[:limit]:
        name = r.get(key) or r.get("name") or r.get("body") or ""
        name = name.strip().split("\n")[0]
        if len(name) > 100:
            name = name[:100] + "…"
        out.append("  - " + name)
    return "\n".join(out)


def _format_gist_block(gists, *, max_chars=8000):
    """Render gists as 'g:<id> [confidence] body' lines, oldest first.
    Truncated to max_chars to bound prompt size."""
    if not gists:
        return "  (none)"
    chrono = sorted(gists, key=lambda g: g.get("created_at") or "")
    lines = []
    used = 0
    for g in chrono:
        body = (g.get("body") or "").strip().replace("\n", " ")
        if len(body) > 400:
            body = body[:400] + "…"
        line = "  g:{} [{}] {}".format(
            g["id"], g.get("confidence") or "med", body,
        )
        if used + len(line) > max_chars:
            lines.append("  …(truncated, {} more gists not shown)".format(
                len(chrono) - len(lines)))
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


_JSON_OBJECT_RX = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def parse_scan_response(text):
    """Pull the {"insights": [...]} object out of the LLM response.

    Tolerates: leading/trailing prose, markdown fences, trailing
    commas (no — JSON is strict; we don't fix), bare arrays.

    Returns: list of insight dicts. Empty list if parse fails or no
    insights; never raises.
    """
    if not text:
        return []
    raw = text.strip()
    # Strip markdown code fences.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)
    # Find the first {...} block.
    m = _JSON_OBJECT_RX.search(raw)
    if not m:
        return []
    blob = m.group(0)
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        log.warning("insight scan: bad JSON: %s", blob[:200])
        return []
    insights = obj.get("insights")
    if not isinstance(insights, list):
        return []
    cleaned = []
    for raw_item in insights:
        if not isinstance(raw_item, dict):
            continue
        kind = (raw_item.get("kind") or "").strip().lower()
        if kind not in ("theme", "pattern", "drift"):
            continue
        title = (raw_item.get("title") or "").strip()
        body = (raw_item.get("body") or "").strip()
        if not title or not body:
            continue
        confidence = (raw_item.get("confidence") or "med").strip().lower()
        # Conservative bar: drop "low" — the prompt says med+ only.
        if confidence == "low":
            continue
        if confidence not in ("med", "high"):
            confidence = "med"
        direction = (raw_item.get("direction") or "").strip().lower()
        if kind == "drift" and direction not in (
                "started", "stopped", "shifted"):
            direction = "shifted"
        elif kind != "drift":
            direction = ""
        rationale = (raw_item.get("rationale") or "").strip()
        sup = raw_item.get("supporting_gist_ids") or []
        if not isinstance(sup, list):
            sup = []
        # Coerce ids to ints, drop garbage.
        clean_sup = []
        for s in sup:
            try:
                clean_sup.append(int(s))
            except (ValueError, TypeError):
                continue
        cleaned.append({
            "kind": kind,
            "title": title[:200],
            "body": body[:2000],
            "confidence": confidence,
            "direction": direction,
            "rationale": rationale[:1000],
            "supporting_gist_ids": clean_sup,
        })
    return cleaned


def scan_project_arcs(
    *, db, llm, project, days=7,
    max_cost_usd=0.05, budget=None,
    triggered_by="manual",
):
    """Run an insight scan on one project's gist arc.

    Returns: dict with {ok, project, candidates_proposed,
    candidates_deduped, gists_seen, cost_usd, scan_id}.
    Logs the scan in insight_scans regardless of outcome.
    """
    project = (project or "").strip()
    if not project:
        return {"ok": False, "error": "project is required"}

    # Budget guard up-front.
    if budget is not None and budget.exhausted():
        return {
            "ok": False,
            "error": "tick budget exhausted before scan",
            "candidates_proposed": 0,
        }

    # Gather window.
    now = datetime.now(timezone.utc)
    window_end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_start_dt = now - timedelta(days=int(days))
    window_start = window_start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_db_fmt = window_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    gists = db.gists_for_project(
        project=project, since_iso=since_db_fmt, limit=200,
    )

    if len(gists) < 2:
        # Not enough material for an arc — log and bail.
        scan_id = db.log_insight_scan(
            scan_kind="gist-arc:project", project=project,
            window_start=window_start, window_end=window_end,
            gists_seen=len(gists), candidates_proposed=0,
            triggered_by=triggered_by, ok=True,
            error="insufficient gists (<2)",
        )
        return {
            "ok": True,
            "project": project,
            "gists_seen": len(gists),
            "candidates_proposed": 0,
            "candidates_deduped": 0,
            "cost_usd": 0.0,
            "scan_id": scan_id,
            "note": "insufficient gists",
        }

    # Existing items to deflect against.
    existing_themes = db.recent_themes(limit=20)
    existing_patterns = db.recent_patterns(limit=30)
    existing_drift = db.recent_drift(limit=30)

    prompt = SCAN_PROMPT_TEMPLATE.format(
        project=project,
        window_start=window_start,
        window_end=window_end,
        gist_count=len(gists),
        existing_themes=_format_existing(existing_themes, key="title"),
        existing_patterns=_format_existing(existing_patterns, key="name"),
        existing_drift=_format_existing(existing_drift, key="body"),
        gist_block=_format_gist_block(gists),
    )

    result = llm.complete(
        prompt,
        purpose="insight-scan",
        max_tokens=1500,
        temperature=0.3,
    )
    if budget is not None:
        budget.charge(result)
    cost_usd = float(result.get("cost_usd") or 0.0)

    if cost_usd > max_cost_usd:
        # Sanity: if a single call somehow exceeded the per-scan cap
        # we still log it (already happened) but warn.
        log.warning(
            "insight scan for %s cost %.4f > cap %.4f",
            project, cost_usd, max_cost_usd,
        )

    if not result.get("ok"):
        scan_id = db.log_insight_scan(
            scan_kind="gist-arc:project", project=project,
            window_start=window_start, window_end=window_end,
            gists_seen=len(gists), candidates_proposed=0,
            cost_usd=cost_usd, triggered_by=triggered_by,
            ok=False, error=result.get("error", "llm error")[:500],
        )
        return {
            "ok": False,
            "project": project,
            "gists_seen": len(gists),
            "candidates_proposed": 0,
            "candidates_deduped": 0,
            "cost_usd": cost_usd,
            "scan_id": scan_id,
            "error": result.get("error", "llm error"),
        }

    insights = parse_scan_response(result.get("text") or "")

    proposed_n = 0
    deduped_n = 0
    proposed_by = "sonnet:insight-scan"
    actual_model = result.get("model") or ""
    if actual_model:
        proposed_by = "{}:insight-scan".format(actual_model)

    for ins in insights:
        new_id = db.insert_pending_interpretation(
            kind=ins["kind"],
            title=ins["title"],
            body=ins["body"],
            confidence=ins["confidence"],
            direction=ins["direction"],
            rationale=ins["rationale"],
            proposed_by=proposed_by,
            source_kind="gist-arc",
            source_project=project,
            source_window_start=window_start,
            source_window_end=window_end,
            source_pointer_ids=ins["supporting_gist_ids"],
        )
        if new_id is None:
            deduped_n += 1
        else:
            proposed_n += 1

    scan_id = db.log_insight_scan(
        scan_kind="gist-arc:project", project=project,
        window_start=window_start, window_end=window_end,
        gists_seen=len(gists), candidates_proposed=proposed_n,
        candidates_deduped=deduped_n, cost_usd=cost_usd,
        triggered_by=triggered_by, ok=True,
    )

    return {
        "ok": True,
        "project": project,
        "gists_seen": len(gists),
        "candidates_proposed": proposed_n,
        "candidates_deduped": deduped_n,
        "cost_usd": cost_usd,
        "scan_id": scan_id,
    }


# ── Slice 3h CP2: project selection for the auto-loop ─────────────


# Policy options for which projects the auto-loop is allowed to scan.
# Manual /insight/scan-now ignores policy and works for any project.
POLICY_HUMAN_ONLY = "active+human"  # default — conservative
POLICY_ALL = "all"                   # includes automation projects
POLICY_OFF = "never"                 # disables auto-loop entirely


def select_eligible_projects(
    *, db, policy=POLICY_HUMAN_ONLY,
    scan_interval_hours=24, max_projects=2,
):
    """Return up to max_projects project tags due for an insight scan.

    Filters by classification policy, then by "last scanned more than
    scan_interval_hours ago." Sorts so the most-stale projects come
    first (the ones that haven't been scanned in the longest time).

    Returns list[str] of project tags, may be empty.
    """
    if policy == POLICY_OFF:
        return []

    settings = db.list_project_settings()
    # Decide which classifications are eligible.
    if policy == POLICY_ALL:
        allowed = {"human", "automation", "auto"}
    else:
        # POLICY_HUMAN_ONLY (default). 'auto' = unclassified yet; allow
        # so first-pass projects don't get stuck out forever. The
        # classifier runs every tick, so they'll soon be auto -> human
        # or auto -> automation.
        allowed = {"human", "auto"}

    eligible = [
        s for s in settings
        if (s.get("treat_as") or "auto") in allowed
        and (s.get("project") or "").strip()
    ]
    if not eligible:
        return []

    # When was each last scanned? Pull the most recent insight_scans
    # row per project. Skip projects scanned within scan_interval_hours.
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=int(scan_interval_hours)))
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    last_scan_by_project = {}
    for row in db.recent_insight_scans(limit=500):
        proj = row.get("project") or ""
        if not proj:
            continue
        # recent_insight_scans is already ordered DESC by scanned_at,
        # so first hit per project wins.
        last_scan_by_project.setdefault(proj, row.get("scanned_at"))

    candidates = []
    for s in eligible:
        proj = s["project"]
        last = last_scan_by_project.get(proj)
        if last and last >= cutoff_str:
            continue  # scanned recently
        candidates.append((proj, last or ""))

    # Sort: never-scanned (last == "") first, then oldest scan ascending.
    # Empty string sorts before any timestamp, which is what we want.
    candidates.sort(key=lambda t: t[1])
    return [proj for proj, _ in candidates[:max_projects]]


# ── Slice 3h CP2: chat-snippet candidate extraction ──────────────


# The overseer's chat persona is instructed to mark insight-worthy
# observations with this fenced block. The block is parsed out of the
# reply BEFORE the reply is shown to the user, so the marker is
# invisible — but the candidate lands in pending_interpretations.
#
#   ```insight
#   {"kind": "pattern", "title": "...", "body": "...",
#    "confidence": "med"}
#   ```
#
# Multiple blocks per reply are supported.

CHAT_INSIGHT_MARKER_INSTRUCTION = """
INSIGHT MARKING — OPTIONAL, USE SPARINGLY

If, in the course of your reply, you genuinely observe a NEW pattern,
drift, or theme about the user that isn't already in your context, you
MAY mark it as a candidate by appending a fenced block at the END of
your reply (after your normal answer):

```insight
{"kind": "pattern", "title": "<noun phrase>", "body": "<1-3 sentences>", "confidence": "med"}
```

Rules:
- ONLY use this when the insight is something you actually noticed in
  THIS exchange that wasn't already a known pattern/theme/drift. Do
  NOT echo what's already in working memory.
- kind must be "pattern", "drift", or "theme".
- For drift, also include "direction": "started" | "stopped" | "shifted".
- confidence must be "med" or "high" (low gets dropped).
- Multiple blocks allowed for multiple distinct candidates.
- The block is invisible to the user; it just queues a candidate for
  their later review. So don't refer to it in your prose.

If nothing rises to that bar — and most replies won't — say nothing.
A blank reply on this front is correct most of the time.
"""


_CHAT_INSIGHT_BLOCK_RX = re.compile(
    r"```insight\s*\n([\s\S]*?)\n```",
    re.IGNORECASE,
)


def parse_chat_snippet_markers(reply_text):
    """Pull every ```insight {...}``` block out of an overseer reply.

    Returns: (cleaned_reply_text, list[insight_dict]).
    Insights are validated/normalized the same way as parse_scan_response.
    """
    if not reply_text:
        return reply_text or "", []

    found = []
    for m in _CHAT_INSIGHT_BLOCK_RX.finditer(reply_text):
        body = m.group(1).strip()
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            log.warning("chat insight: bad JSON in marker: %s", body[:200])
            continue
        if not isinstance(obj, dict):
            continue
        kind = (obj.get("kind") or "").strip().lower()
        if kind not in ("theme", "pattern", "drift"):
            continue
        title = (obj.get("title") or "").strip()
        body_text = (obj.get("body") or "").strip()
        if not title or not body_text:
            continue
        confidence = (obj.get("confidence") or "med").strip().lower()
        if confidence == "low":
            continue
        if confidence not in ("med", "high"):
            confidence = "med"
        direction = (obj.get("direction") or "").strip().lower()
        if kind == "drift" and direction not in (
                "started", "stopped", "shifted"):
            direction = "shifted"
        elif kind != "drift":
            direction = ""
        rationale = (obj.get("rationale") or "").strip()
        found.append({
            "kind": kind,
            "title": title[:200],
            "body": body_text[:2000],
            "confidence": confidence,
            "direction": direction,
            "rationale": rationale[:1000],
        })

    cleaned = _CHAT_INSIGHT_BLOCK_RX.sub("", reply_text).rstrip()
    return cleaned, found


def extract_and_queue_chat_insights(
    *, db, reply_text, chat_message_id=None,
    proposed_by="overseer:chat-snippet",
):
    """Parse insight markers from a chat reply and insert them as
    pending interpretations.

    Returns: (cleaned_reply_text, list of {ok, interp_id, kind, title}).
    cleaned_reply_text is what to show the user (markers stripped).
    """
    cleaned, candidates = parse_chat_snippet_markers(reply_text)
    if not candidates:
        return cleaned, []

    queued = []
    for c in candidates:
        new_id = db.insert_pending_interpretation(
            kind=c["kind"],
            title=c["title"],
            body=c["body"],
            confidence=c["confidence"],
            direction=c["direction"],
            rationale=c["rationale"],
            proposed_by=proposed_by,
            source_kind="chat-snippet",
            source_chat_message_id=chat_message_id,
        )
        queued.append({
            "ok": True,
            "interp_id": new_id,         # None means deduped
            "kind": c["kind"],
            "title": c["title"],
            "deduped": new_id is None,
        })
    return cleaned, queued


def apply_pending_interpretation(*, db, interp_id, decision,
                                  reviewed_by="user", review_note="",
                                  edit_title="", edit_body=""):
    """Confirm / reject / edit a pending interpretation.

    On 'confirm' or 'edit-and-confirm', the candidate becomes a real
    row in patterns / drift_observations / summaries_theme. Returns
    the post-action row, including applied_table+applied_id when
    confirmed.
    """
    row = db.get_pending_interpretation(interp_id)
    if not row:
        return {"ok": False, "error": "not found"}
    if row["status"] != "pending":
        return {
            "ok": False,
            "error": "already in status {!r}".format(row["status"]),
        }

    decision = (decision or "").strip().lower()
    if decision == "reject":
        db.update_pending_interpretation_status(
            interp_id=interp_id, status="rejected",
            reviewed_by=reviewed_by, review_note=review_note,
        )
        return {"ok": True, "interp_id": interp_id, "status": "rejected"}

    if decision not in ("confirm", "edit-and-confirm"):
        return {"ok": False, "error": "decision must be confirm | reject | edit-and-confirm"}

    title = (edit_title or row["title"]).strip()
    body = (edit_body or row["body"]).strip()
    kind = row["kind"]
    confidence = row["confidence"]
    direction = row["direction"]
    # Don't fabricate a raw_pointers row at confirm-time. The
    # supporting_gist_ids stay on the pending row (source_pointer_ids),
    # and a future drill-down can walk back to them via the pending
    # interpretation's id. raw_pointer_id is reserved for raw_pointers
    # entries (a separate table), not gist ids — passing a gist id
    # there violates the FK.

    applied_table = ""
    applied_id = None
    project_tag = ("project:" + row["source_project"]
                   if row.get("source_project") else None)
    # Tag with both the project AND a marker noting the source so a
    # future query can find "all interpretations born in slice 3h".
    tags = ["from:insight-scan"]
    if project_tag:
        tags.append(project_tag)

    if kind == "pattern":
        applied_table = "patterns"
        applied_id = db.add_pattern(
            name=title, body=body, confidence=confidence, tags=tags,
        )
    elif kind == "drift":
        applied_table = "drift_observations"
        applied_id = db.add_drift(
            body=body, direction=direction, confidence=confidence,
            tags=tags,
        )
    elif kind == "theme":
        applied_table = "summaries_theme"
        applied_id = db.add_theme(
            title=title, body=body, confidence=confidence, tags=tags,
        )
    elif kind == "blindspot":
        # 3i CP2: blindspot proposals carry their model/topic patterns
        # in the bs_* columns. Direction is reused (downgrades|overstates|
        # misses|hedges|general). Confidence-adjustment is bs_* specific.
        applied_table = "known_blindspots"
        applied_id = db.upsert_blindspot(
            id=None,
            model_pattern=row.get("bs_model_pattern") or "*",
            body=body,
            topic_pattern=row.get("bs_topic_pattern") or "",
            direction=direction or "general",
            confidence_adjustment=int(row.get("bs_confidence_adjustment") or 0),
            rationale=row.get("rationale") or "",
            confidence=confidence,
            source="auto-proposed",
            is_active=True,
        )
        # Link this blindspot back to the corrections that generated
        # it. The pending row's source_pointer_ids carries correction
        # ids in the blindspot case (rather than gist ids).
        try:
            ids_json = row.get("source_pointer_ids") or "[]"
            correction_ids = json.loads(ids_json) if ids_json else []
            if correction_ids:
                db.mark_corrections_distilled(
                    correction_ids=correction_ids,
                    blindspot_id=applied_id,
                )
        except (json.JSONDecodeError, ValueError):
            pass
    else:
        return {"ok": False, "error": "unknown kind: " + kind}

    new_status = "edited" if decision == "edit-and-confirm" else "confirmed"
    db.update_pending_interpretation_status(
        interp_id=interp_id, status=new_status,
        reviewed_by=reviewed_by, review_note=review_note,
        edit_title=edit_title or "", edit_body=edit_body or "",
        applied_table=applied_table, applied_id=applied_id,
    )

    return {
        "ok": True,
        "interp_id": interp_id,
        "status": new_status,
        "applied_table": applied_table,
        "applied_id": applied_id,
    }
