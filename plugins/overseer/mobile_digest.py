"""Mobile capture digest: fold each day of phone notes into the pipeline.

Step 1c.7 (2026-06-12). Phone-captured notes arrive via the sync plugin
with source='mobile' and no session_id, so the session-driven gist step
never sees them: they were tagged and raw-searchable but never gisted,
embedded, or routed against open questions. Per the locked pipeline
vision (2026-06-11), the user's own captures are the HIGHEST-value
content in the corpus; this step gives them the full treatment.

Behavior:
  - Gathers sessionless source='mobile' notes from cortex.db.
  - Groups by COMPLETE local day (today is skipped so the digest always
    sees the whole day; local_created_at preferred, slice 9.4.1).
  - One gist per day, period_label 'mobile-notes:<YYYY-MM-DD>', via the
    standard summarize-session model tier and THE CHANGE framing.
  - Routes the gist against active open questions (question_routing).
  - Embeddings arrive via the existing missing-embeddings backfill.
  - High-water mark in overseer_state ('mobile_digest_done_through'),
    so each day is digested exactly once.
"""

from __future__ import annotations

from datetime import datetime

from prompts import mobile_digest_prompt
from question_routing import route_evidence_to_questions

STATE_KEY = "mobile_digest_done_through"
MAX_BODY_CHARS = 8000
MAX_DAYS_PER_RUN = 7


def _local_day(row: dict) -> str:
    return ((row.get("local_created_at") or row.get("created_at") or "")[:10])


def run_mobile_digest(*, core, db, llm, budget=None, log=None,
                      summary: dict | None = None) -> dict:
    """core: CoreMemoryRO (cortex.db, read-only). db: OverseerDB.
    Returns {ok, days_digested, gist_ids, skipped_reason?}."""
    out = {"ok": True, "days_digested": 0, "gist_ids": []}
    rows = core.query(
        "SELECT id, content, note_type, project, created_at, "
        "local_created_at FROM notes "
        "WHERE source = 'mobile' AND (session_id IS NULL OR session_id = '') "
        "ORDER BY created_at")
    if not rows:
        return out

    # Owner's calendar day, not the container's: in a UTC container
    # host-today outruns owner-today from evening on, which would let
    # the IN-PROGRESS day pass the d < today_local guard and get
    # digested half-finished (tenant-TZ pass, cloud P2 2026-07-20).
    from temporal import tenant_tz
    _tz = tenant_tz()
    today_local = ((datetime.now(_tz) if _tz is not None
                    else datetime.now().astimezone())
                   .strftime("%Y-%m-%d"))
    done_through = str(db.get_overseer_state(STATE_KEY, "") or "")
    days = sorted({d for d in (_local_day(r) for r in rows) if d})
    todo = [d for d in days if d > done_through and d < today_local]
    todo = todo[:MAX_DAYS_PER_RUN]

    for day in todo:
        if budget is not None and budget.exhausted():
            out["skipped_reason"] = "budget exhausted"
            break
        notes = [r for r in rows if _local_day(r) == day]
        lines = []
        for r in notes:
            ts = (r.get("local_created_at") or r.get("created_at") or "")[11:16]
            kind = r.get("note_type") or "note"
            proj = (" proj:" + r["project"]) if r.get("project") else ""
            lines.append("[{}] ({}{}) {}".format(
                ts, kind, proj, (r.get("content") or "").strip()))
        body = "\n".join(lines)[:MAX_BODY_CHARS]

        prompt = mobile_digest_prompt(day=day, n_notes=len(notes), body=body)
        result = llm.complete(prompt, max_tokens=200, temperature=0.4,
                              purpose="summarize-session")
        if budget is not None:
            budget.charge(result)
        if not result.get("ok"):
            if log:
                log.warning("mobile digest %s failed: %s",
                            day, result.get("error"))
            out["ok"] = False
            break
        gist_text = (result.get("text") or "").strip().strip('"').strip()
        if not gist_text:
            # Empty reply: advance the mark anyway so one bad day cannot
            # wedge the queue forever; the raw notes remain searchable.
            db.set_overseer_state(STATE_KEY, day)
            continue

        gid = db.add_gist(
            gist_text,
            period_label="mobile-notes:{}".format(day),
            period_start="{} 00:00:00".format(day),
            period_end="{} 23:59:59".format(day),
            confidence="med",
            tags=["auto", "mobile-digest", "source:mobile"],
        )
        out["gist_ids"].append(gid)
        out["days_digested"] += 1
        db.set_overseer_state(STATE_KEY, day)

        try:
            route_evidence_to_questions(
                db=db, llm=llm, gist_text=gist_text, gist_id=gid,
                budget=budget, contributed_by="auto:mobile-digest")
        except Exception as e:
            if log:
                log.warning("mobile digest routing %s failed: %s", day, e)

        if summary is not None:
            summary.setdefault("mobile_digests", []).append(
                {"day": day, "gist_id": gid, "notes": len(notes)})

    return out
