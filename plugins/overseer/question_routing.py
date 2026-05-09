"""Question-centered routing — file new evidence against the user's
living questions.

Per locked design (Tory's meta-layer review #2):

  "Continuity should be structured around questions, not events.
  Questions persist across years; events are evidence relevant to
  questions. The overseer's job is to maintain the questions and
  route new evidence to them."

Every new interpretive artifact (gist primarily; episodes/themes
when 3h synthesis adds them) gets routed via Sonnet 4.6 against the
currently-active open_questions. The model returns zero, one, or
many `Q<N>: <contribution> | <reason>` lines, OR a single
`unfiled` line when nothing meaningfully connects.

Be conservative: most operational gists ("shipped v1.15.0") don't
speak to deep questions. Forcing a connection corrupts the
question-centered view. Unfiled is the right answer when nothing
real is there.

Cost: ~$0.005 per routing call (Sonnet, small structured task).
"""

from __future__ import annotations

import logging
import re
from typing import Any


log = logging.getLogger("plugin.overseer.question_routing")


VALID_CONTRIBUTIONS = ("supports", "complicates", "answers", "reframes")


# ── Prompt ──────────────────────────────────────────────────────

ROUTING_PROMPT_TEMPLATE = """\
You are routing a new piece of evidence against the user's open \
questions — the long-running concerns the overseer tracks across \
time.

Open questions:
{questions}

New evidence to route:
{evidence}

For each question this evidence MEANINGFULLY speaks to (zero, one, \
or many — being conservative is correct), reply with one line:
  Q<N>: <contribution> | <one-sentence reason>

Where contribution is one of:
  supports    = adds weight to one possible reading
  complicates = challenges or muddies a possible reading
  answers     = resolves the question (rare, be careful — the user
                always has final say on whether something "answers")
  reframes    = changes how the question itself is understood

If the evidence doesn't meaningfully speak to any open question, \
reply with a single line:
  unfiled

Be conservative. Most gists are operational and don't connect to \
deep questions; that's expected and correct. Forcing connections \
corrupts the question-centered view. Empty is the right answer when \
nothing real is there.

Reply only with routing lines or 'unfiled'. No preamble. No headings."""


def build_routing_prompt(*, gist_text: str,
                         questions: list[dict]) -> str:
    """Compose the routing prompt. `questions` is the list returned by
    db.active_questions() — each must have id/question/confidence."""
    qlines = []
    for i, q in enumerate(questions, 1):
        body = (q.get("body") or "").strip()
        body_part = " — {}".format(body[:160]) if body else ""
        qlines.append("Q{}. [{}] {}{}".format(
            i, q.get("confidence", "med"),
            q.get("question", "(unknown)"), body_part))
    return ROUTING_PROMPT_TEMPLATE.format(
        questions="\n".join(qlines) if qlines else "(no open questions)",
        evidence=gist_text.strip(),
    )


# ── Parser ──────────────────────────────────────────────────────

_LINE_RX = re.compile(
    r"^\s*Q(\d+)\s*:\s*(\w+)\s*(?:\|\s*(.+))?$",
    re.IGNORECASE,
)


def parse_routing_response(text: str, n_questions: int) -> list[dict]:
    """Parse the LLM's routing reply into a list of decisions.

    Returns list of {question_index, contribution, reason} (1-based
    indices). Empty list = unfiled or unparseable.

    Tolerates:
      - leading "unfiled" (case-insensitive) → returns []
      - extra prose lines (silently dropped)
      - duplicate Q references (last one wins)
      - whitespace, trailing punctuation
    """
    if not text:
        return []
    raw = text.strip()
    # First non-empty line decides if we bail with unfiled
    first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()),
                      "")
    if first_line.lower().startswith("unfiled"):
        return []

    seen: dict[int, dict] = {}  # last-write-wins per question
    for line in raw.splitlines():
        m = _LINE_RX.match(line.strip())
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        if not (1 <= idx <= n_questions):
            continue
        contribution = m.group(2).lower().strip()
        if contribution not in VALID_CONTRIBUTIONS:
            continue
        reason = (m.group(3) or "").strip().rstrip(".").strip()
        seen[idx] = {
            "question_index": idx,
            "contribution": contribution,
            "reason": reason[:300],
        }
    return list(seen.values())


# ── Main entry ──────────────────────────────────────────────────

def route_evidence_to_questions(*, db, llm, gist_text: str,
                                gist_id: int,
                                budget=None,
                                contributed_by="auto:sonnet"
                                ) -> dict:
    """Route a single gist against open questions and file evidence
    rows. Returns a summary dict.

    `gist_text` should be the body of the gist being routed.
    `gist_id` is the row id in summaries_gist (the file_evidence
    target).

    Cheap: one Sonnet call per gist, ~$0.005. Skips entirely if
    there are no active questions to route against.
    """
    questions = db.active_questions(limit=20)
    if not questions:
        return {
            "ok": True, "skipped": "no active questions",
            "filings": [], "reactivated": [],
        }
    if budget is not None and budget.exhausted():
        return {
            "ok": False, "skipped": "budget exhausted",
            "filings": [], "reactivated": [],
        }

    prompt = build_routing_prompt(
        gist_text=gist_text, questions=questions)
    try:
        result = llm.complete(
            prompt, max_tokens=400, temperature=0.2,
            purpose="evidence-routing",
        )
    except Exception as e:
        log.warning("routing LLM call failed: %s", e)
        return {"ok": False, "error": str(e),
                "filings": [], "reactivated": []}
    if budget is not None:
        budget.charge(result)
    if not result.get("ok"):
        return {"ok": False,
                "error": result.get("error", "")[:300],
                "filings": [], "reactivated": []}

    decisions = parse_routing_response(
        result.get("text") or "", len(questions))

    if not decisions:
        return {
            "ok": True, "result": "unfiled",
            "filings": [], "reactivated": [],
            "raw_response": (result.get("text") or "")[:300],
            "cost_usd": result.get("cost_usd", 0),
        }

    filings = []
    reactivated = []
    for d in decisions:
        q = questions[d["question_index"] - 1]
        try:
            filed, react = db.file_evidence(
                question_id=q["id"],
                evidence_table="summaries_gist",
                evidence_id=gist_id,
                contribution=d["contribution"],
                reason=d["reason"],
                confidence="med",
                contributed_by=contributed_by,
            )
            filings.append({
                "question_id": q["id"],
                "question": q["question"],
                "contribution": d["contribution"],
                "reason": d["reason"],
                "newly_filed": filed,
            })
            if react:
                reactivated.append({
                    "question_id": q["id"],
                    "question": q["question"],
                })
        except Exception as e:
            log.warning("file_evidence failed for q=%s gist=%s: %s",
                        q["id"], gist_id, e)
    return {
        "ok": True,
        "filings": filings,
        "reactivated": reactivated,
        "decisions_n": len(decisions),
        "cost_usd": result.get("cost_usd", 0),
    }
