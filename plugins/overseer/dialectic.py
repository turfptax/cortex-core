"""Paired-generation dialectic checker.

Per locked design (Tory, 2026-05-02 meta-layer review):

  "No trust in singletons. The dialectic should be public."

For every interpretive artifact (gist / theme / episode / question
identification), generate the artifact via TWO models in parallel —
not Opus-writes-then-Gemma-critiques, which leaves the second model
responding to a frame the first model already established. Both
models see the same source, generate independently, and the diff
between their outputs is the data that 3f.5's Public Dialectic UI
surfaces.

Default pairing: Opus 4.7 + Gemma 3 27B (via the LLMRouter's
"dialectic-check" purpose for Gemma; Opus is the existing default
for high-stakes interpretive work).

Severity classification is heuristic (text similarity + length diff):
  - >= 0.85 similarity                       → "none"  (cosmetic)
  - 0.55 - 0.85 similarity                   → "minor" (different emphasis)
  - < 0.55 similarity                        → "significant" (different framing)
Plus length-mismatch escalation: if one is >2x the other, bump up.

A future iteration can use Sonnet-as-judge for a more accurate
severity call; heuristic is fine for the dialectic table to populate.
"""

from __future__ import annotations

import logging
import threading
from difflib import SequenceMatcher
from typing import Any


log = logging.getLogger("plugin.overseer.dialectic")


# Default model slugs. Gemma is set via [llm.model_overrides]
# "dialectic-check" too, but we hardcode here as a fallback.
DEFAULT_OPUS_MODEL = "anthropic/claude-opus-4.7"
DEFAULT_GEMMA_MODEL = "google/gemma-3-27b-it"


def paired_generate(*, llm, prompt: str, system: str | None = None,
                    max_tokens: int = 200, temperature: float = 0.4,
                    purpose: str = "summarize-session",
                    opus_model: str | None = None,
                    gemma_model: str | None = None,
                    timeout_s: float = 90.0) -> dict:
    """Run the SAME prompt through Opus 4.7 and Gemma 3 in parallel.

    Returns a dict shaped like:
        {
            "opus": <llm_router result dict>,
            "gemma": <llm_router result dict>,
            "diff": {"severity": str, "similarity": float,
                     "len_diff_ratio": float, "summary": str},
            "ok": bool,    # both succeeded
        }

    Both calls are charged to llm_calls separately by the router (each
    has its own cost row + per-call latency). The caller is responsible
    for charging budget twice if it cares to count both.
    """
    opus_m = opus_model or DEFAULT_OPUS_MODEL
    gemma_m = gemma_model or DEFAULT_GEMMA_MODEL

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def _call(label, model, call_purpose):
        try:
            results[label] = llm.complete(
                prompt, system=system,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                purpose=call_purpose,
            )
        except Exception as e:
            errors[label] = str(e)
            results[label] = {
                "ok": False, "error": str(e), "text": "",
                "backend": "", "model": model, "latency_ms": 0,
                "cost_usd": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0,
            }

    t_opus = threading.Thread(
        target=_call,
        args=("opus", opus_m, purpose),
        name="paired-opus",
    )
    t_gemma = threading.Thread(
        target=_call,
        args=("gemma", gemma_m, "dialectic-check"),
        name="paired-gemma",
    )
    t_opus.start()
    t_gemma.start()
    t_opus.join(timeout=timeout_s)
    t_gemma.join(timeout=timeout_s)

    opus_r = results.get("opus") or {
        "ok": False, "error": "opus thread did not complete",
        "text": "", "backend": "", "model": opus_m, "cost_usd": 0.0,
    }
    gemma_r = results.get("gemma") or {
        "ok": False, "error": "gemma thread did not complete",
        "text": "", "backend": "", "model": gemma_m, "cost_usd": 0.0,
    }

    diff = compute_diff(opus_r, gemma_r)
    return {
        "opus": opus_r,
        "gemma": gemma_r,
        "diff": diff,
        "ok": bool(opus_r.get("ok") and gemma_r.get("ok")),
    }


def compute_diff(opus_result: dict, gemma_result: dict) -> dict:
    """Heuristic diff classifier. Returns severity + similarity + summary.

    Severity buckets (for the dialectic_open.severity column):
      - "none"        — outputs are essentially the same
      - "minor"       — different emphasis or hedging, same direction
      - "significant" — different framing or contradictory claims
    """
    opus_text = (opus_result.get("text") or "").strip()
    gemma_text = (gemma_result.get("text") or "").strip()

    # If either failed, the diff is degenerate but record it
    if not opus_result.get("ok") or not gemma_result.get("ok"):
        return {
            "severity": "none",
            "similarity": 0.0,
            "len_diff_ratio": 0.0,
            "summary": "one or both models failed; no comparison",
        }
    if not opus_text or not gemma_text:
        return {
            "severity": "none",
            "similarity": 0.0,
            "len_diff_ratio": 0.0,
            "summary": "one or both responses were empty",
        }

    sim = SequenceMatcher(
        None, opus_text.lower(), gemma_text.lower(),
    ).ratio()

    len_o = len(opus_text)
    len_g = len(gemma_text)
    len_diff_ratio = (max(len_o, len_g) / min(len_o, len_g)
                      if min(len_o, len_g) > 0 else 0.0)

    # Base severity from similarity
    if sim >= 0.85:
        severity = "none"
    elif sim >= 0.55:
        severity = "minor"
    else:
        severity = "significant"

    # Escalation: length mismatch >= 2x → bump up one notch
    if len_diff_ratio >= 2.0:
        if severity == "none":
            severity = "minor"
        elif severity == "minor":
            severity = "significant"

    summary_parts = []
    summary_parts.append("similarity {:.2f}".format(sim))
    if len_diff_ratio > 1.5:
        summary_parts.append("length ratio {:.1f}x".format(len_diff_ratio))
    if severity == "significant":
        summary_parts.append("framings diverge")
    elif severity == "minor":
        summary_parts.append("emphasis differs")
    else:
        summary_parts.append("substantively aligned")

    return {
        "severity": severity,
        "similarity": round(sim, 3),
        "len_diff_ratio": round(len_diff_ratio, 2),
        "summary": "; ".join(summary_parts),
    }


def write_dialectic_row(*, db, paired: dict, artifact_type: str,
                        artifact_id: int | None,
                        purpose: str = "",
                        source_context: str = "") -> int | None:
    """Persist the paired result as a dialectic_open row.

    Always writes a row when both models returned text (regardless of
    severity) so the table is the full record of what each model said
    on every artifact, not just disagreements. Filtering for "show me
    only significant disagreements" is a query-time concern.

    Returns the new row id, or None if there was nothing to write
    (both failed, or both empty).
    """
    diff = paired.get("diff") or {}
    opus_r = paired.get("opus") or {}
    gemma_r = paired.get("gemma") or {}

    opus_text = (opus_r.get("text") or "").strip()
    gemma_text = (gemma_r.get("text") or "").strip()
    if not opus_text and not gemma_text:
        return None

    return db.add_dialectic(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        purpose=purpose,
        opus_model=opus_r.get("model", "") or DEFAULT_OPUS_MODEL,
        gemma_model=gemma_r.get("model", "") or DEFAULT_GEMMA_MODEL,
        opus_text=opus_text,
        gemma_text=gemma_text,
        opus_confidence="med",   # self-report parsing comes in 3f.5
        gemma_confidence="med",
        severity=diff.get("severity", "none"),
        similarity=diff.get("similarity", 1.0),
        diff_summary=diff.get("summary", ""),
        source_context=source_context[:4000] if source_context else "",
        opus_cost_usd=float(opus_r.get("cost_usd") or 0.0),
        gemma_cost_usd=float(gemma_r.get("cost_usd") or 0.0),
    )
