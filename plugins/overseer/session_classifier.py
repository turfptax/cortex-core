"""Session NATURE classifier: what kind of conversation is this?

Proposed Stage 0.5 of the interpretation pipeline (2026-06-11, Tory
directive): before any gist or abstraction work, classify each raw
conversation so mechanical, high-volume threads can be down-weighted and
real human-driven conversations weighted properly.

This is deliberately ORTHOGONAL to the two classifiers that already exist:
  - category_classifier.py answers "what TOPIC?" (work / cortex / personal)
  - overseer_db.auto_classify_projects() answers "is this PROJECT mostly
    automation?" (coarse, per-project median heuristic)
This module answers "what NATURE is this one SESSION?", refining the
Slice 3e treat_as vocabulary (human / automation / ignore) to session
granularity. Its `treatment` output maps onto the existing pipeline
branches: gist / rollup / skip.

Design: deterministic signals first, LLM only on ambiguity.
  1. extract_signals(): cheap, explainable features from the parsed
     messages (structure, ratios, lengths, scheduling markers).
  2. score_categories(): transparent additive rules; every point carries
     the signal that produced it, so a human can audit the decision.
  3. classify_session(): if the rule margin is thin, optionally escalate
     to one Flash call (purpose 'session-classify', ~$0.001) that sees the
     signals plus a small transcript snippet and picks a category.

Categories:
  human-dialogue     the user thinking/talking (philosophy, planning,
                     creative work, life). Highest memory value.
  human-build        user-directed work session, assistant executes
                     (typical Claude Code dev session). Normal value.
  automation-checkin scheduled/recurring system sessions (overseer
                     check-ins, health checks, cron-shaped). Low value,
                     high volume; belongs in rollups.
  automation-batch   programmatic runs with negligible human input
                     (imports, pipelines, agent-to-agent traffic).
                     Lowest value; rollup or skip.

NOT wired into loop.py yet, per the study-first directive: Pipeline Lab
exposes it for inspection; production wiring is a separate decision.
"""

from __future__ import annotations

import json
import re
import statistics

CATEGORIES = ("human-dialogue", "human-build", "automation-checkin", "automation-batch")

# category -> (memory weight 0..1, pipeline treatment)
TREATMENT = {
    "human-dialogue": (1.0, "gist"),
    "human-build": (0.8, "gist"),
    "automation-checkin": (0.2, "rollup"),
    "automation-batch": (0.1, "rollup-or-skip"),
}

# Markers that betray a scheduled / harness-driven opener.
_SCHEDULED_RX = re.compile(
    r"<scheduled-task|\[SYSTEM NOTIFICATION|<task-notification|"
    r"CHECKIN slot|automated background-task|<system-reminder>|"
    r"\bcron\b|This is an automated",
    re.IGNORECASE)

_TOOL_RESULT_PREFIX = "[tool_result"


def extract_signals(metadata: dict, messages: list[dict]) -> dict:
    """Cheap, explainable features. Every value here is shown to the
    human in Pipeline Lab; keep them interpretable."""
    n_user = n_assistant = tool_msgs = 0
    human_texts: list[str] = []
    for m in messages:
        role = m.get("role")
        text = (m.get("content_text") or "").strip()
        if role == "assistant":
            n_assistant += 1
            if m.get("has_tool_use"):
                tool_msgs += 1
        elif role == "user":
            n_user += 1
            # Tool results echo back as user-role messages; they are
            # machine traffic, not the human typing.
            if text and not text.startswith(_TOOL_RESULT_PREFIX):
                human_texts.append(text)

    n_total = len(messages)
    n_human = len(human_texts)
    opener = human_texts[0] if human_texts else ""
    scheduled_hits = sum(
        1 for t in human_texts[:5] if _SCHEDULED_RX.search(t))
    uniq_human = len({t[:200] for t in human_texts}) if human_texts else 0

    return {
        "source": metadata.get("source") or "claude-code",
        "messages_total": n_total,
        "user_messages": n_user,
        "assistant_messages": n_assistant,
        "human_messages": n_human,
        "human_share": round(n_human / n_total, 3) if n_total else 0.0,
        "tool_use_fraction": round(tool_msgs / n_assistant, 3) if n_assistant else 0.0,
        "median_human_chars": int(statistics.median([len(t) for t in human_texts])) if human_texts else 0,
        "unique_human_ratio": round(uniq_human / n_human, 3) if n_human else 0.0,
        "scheduled_markers_in_openers": scheduled_hits,
        "opener_is_template": bool(opener[:1] in "<[" or _SCHEDULED_RX.search(opener[:400])),
        "duration_minutes": int(metadata.get("duration_minutes") or 0),
    }


def score_categories(s: dict) -> tuple[dict, list[dict]]:
    """Transparent additive rules. Returns (scores, contributions);
    each contribution = {signal, category, points, note}."""
    scores = {c: 0.0 for c in CATEGORIES}
    contrib: list[dict] = []

    def add(category, points, signal, note):
        scores[category] += points
        contrib.append({"signal": signal, "category": category,
                        "points": points, "note": note})

    if s["scheduled_markers_in_openers"]:
        add("automation-checkin", 0.55, "scheduled_markers_in_openers",
            "scheduled-task / system-notification markers in the opening turns")
    if s["opener_is_template"]:
        add("automation-checkin", 0.15, "opener_is_template",
            "first human turn starts with a template/harness tag")
    if s["unique_human_ratio"] and s["unique_human_ratio"] < 0.6:
        add("automation-checkin", 0.15, "unique_human_ratio",
            "human turns repeat (templated prompts)")

    if s["human_messages"] == 0:
        add("automation-batch", 0.6, "human_messages",
            "no human-typed turns at all")
    elif s["human_messages"] <= 2 and s["messages_total"] >= 20:
        add("automation-batch", 0.35, "human_messages",
            "long session with almost no human turns")
    if s["tool_use_fraction"] >= 0.5 and s["human_messages"] <= 2:
        add("automation-batch", 0.15, "tool_use_fraction",
            "tool-dominated with no human steering")

    if s["human_messages"] >= 5 and s["tool_use_fraction"] >= 0.3:
        add("human-build", 0.45, "tool_use_fraction",
            "human steering a tool-heavy work session")
    if s["human_messages"] >= 3 and 0.05 < s["tool_use_fraction"] < 0.3:
        add("human-build", 0.15, "tool_use_fraction",
            "some tool use under human direction")

    if s["human_messages"] >= 3 and s["tool_use_fraction"] <= 0.05:
        add("human-dialogue", 0.45, "tool_use_fraction",
            "conversation with essentially no tool traffic")
    if s["median_human_chars"] >= 200:
        add("human-dialogue", 0.15, "median_human_chars",
            "long, composed human messages")
    if s["human_share"] >= 0.4 and s["human_messages"] >= 3:
        add("human-dialogue", 0.15, "human_share",
            "human turns dominate the session")
    if s["source"] not in ("claude-code",) and s["human_messages"] >= 3 \
            and not s["scheduled_markers_in_openers"]:
        add("human-dialogue", 0.1, "source",
            "web-AI source (no tool harness)")

    return scores, contrib


CLASSIFY_PROMPT_TEMPLATE = """\
You classify ONE AI conversation by its NATURE for a personal memory
system. The categories:

  human-dialogue     the user thinking, talking, creating, deciding
  human-build        the user directing a hands-on work session
                     (assistant runs tools, user steers)
  automation-checkin a scheduled or recurring system session
                     (check-ins, health checks, harness-driven)
  automation-batch   programmatic traffic with negligible human input

Structural signals already measured:
{signals}

Transcript sample (openers and a middle slice):
{snippet}

Reply with one JSON object only, no prose:
{{"category": "<one of the four>", "confidence": 0.0-1.0,
  "reason": "<one sentence naming the deciding evidence>"}}"""

_JSON_RX = re.compile(r"\{[\s\S]*\}")


def build_llm_snippet(messages: list[dict], max_chars: int = 1800) -> str:
    human = [
        (m.get("content_text") or "").strip() for m in messages
        if m.get("role") == "user"
        and not (m.get("content_text") or "").startswith(_TOOL_RESULT_PREFIX)
        and (m.get("content_text") or "").strip()
    ]
    picks = human[:2] + (human[len(human) // 2: len(human) // 2 + 1]
                         if len(human) > 4 else [])
    out = []
    used = 0
    for t in picks:
        t = t[:600]
        if used + len(t) > max_chars:
            break
        out.append("HUMAN: " + t)
        used += len(t)
    return "\n\n".join(out) or "(no human turns)"


def parse_classify_response(text: str) -> dict | None:
    if not text:
        return None
    m = _JSON_RX.search(text.strip())
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    cat = (obj.get("category") or "").strip().lower()
    if cat not in CATEGORIES:
        return None
    try:
        conf = max(0.0, min(1.0, float(obj.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    return {"category": cat, "confidence": round(conf, 2),
            "reason": str(obj.get("reason") or "")[:300]}


def classify_session(metadata: dict, messages: list[dict], *,
                     llm=None, llm_margin: float = 0.15) -> dict:
    """Full classification. If `llm` is provided and the deterministic
    margin is thin, one Flash call breaks the tie. Returns everything a
    human needs to audit the decision."""
    signals = extract_signals(metadata, messages)
    scores, contrib = score_categories(signals)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top_cat, top = ranked[0]
    second = ranked[1][1]
    total = sum(scores.values()) or 1.0
    confidence = round(top / total, 2) if top else 0.25
    margin = round(top - second, 2)
    ambiguous = top == 0 or margin < llm_margin

    result = {
        "category": top_cat if top else "human-dialogue",
        "confidence": confidence,
        "margin": margin,
        "method": "rules",
        "signals": signals,
        "scores": {k: round(v, 2) for k, v in scores.items()},
        "contributions": contrib,
        "ambiguous": ambiguous,
        "llm": None,
    }

    if ambiguous and llm is not None:
        prompt = CLASSIFY_PROMPT_TEMPLATE.format(
            signals=json.dumps(signals, indent=1),
            snippet=build_llm_snippet(messages))
        resp = llm.complete(prompt, purpose="session-classify",
                            max_tokens=200, temperature=0.1)
        parsed = parse_classify_response(resp.get("text") or "")
        result["llm"] = {
            "prompt": prompt,
            "model": resp.get("model"),
            "cost_usd": resp.get("cost_usd"),
            "raw_response": resp.get("text"),
            "parsed": parsed,
        }
        if parsed:
            result["category"] = parsed["category"]
            result["confidence"] = parsed["confidence"]
            result["method"] = "rules+llm"

    weight, treatment = TREATMENT[result["category"]]
    result["weight"] = weight
    result["treatment"] = treatment
    return result
