"""Known blindspots — meta-honesty layer.

Per locked design (Tory's meta-layer review #4):

  "Every model has known weaknesses. The overseer should know its
  own weakness profile and apply it as a meta-filter. The user gets
  to calibrate, not just consume."

This module:
  - Matches blindspots against (model, topic) tuples using glob-style
    model patterns and substring-based topic patterns.
  - Returns relevant blindspots for surfacing as caveats next to
    interpretations in working memory and chat context.
  - Records applications (apply_count + last_applied_at) so the
    UI can prioritize frequently-firing blindspots later.

Pattern semantics:
  model_pattern: glob with * wildcard (case-insensitive)
    "*"           → matches any model
    "*opus*"      → matches anything containing 'opus'
    "anthropic/claude-opus-4.7" → exact match

  topic_pattern: pipe-separated keywords (case-insensitive substring)
    ""            → matches any topic
    "UAP|UFO"     → matches if topic contains 'UAP' OR 'UFO'
    "wellbeing|isolation|burnout"  → any of these substrings

Hand-authored seed lives in OverseerDB._seed_blindspots_if_empty.
Future correction-feedback loop adds entries automatically (deferred
to slice 3i).
"""

from __future__ import annotations

import fnmatch
import logging


log = logging.getLogger("plugin.overseer.blindspots")


def _matches_model(pattern: str, model: str) -> bool:
    """Glob-style model match. Case-insensitive."""
    if not pattern:
        return False
    if not model:
        return pattern == "*"
    return fnmatch.fnmatchcase(model.lower(), pattern.lower())


def _matches_topic(pattern: str, topic: str) -> bool:
    """Empty pattern matches any topic. Otherwise pipe-separated
    case-insensitive substring match."""
    if not pattern:
        return True
    if not topic:
        return False
    keywords = [p.strip().lower() for p in pattern.split("|") if p.strip()]
    if not keywords:
        return True
    haystack = topic.lower()
    return any(k in haystack for k in keywords)


def applicable_blindspots(*, db, model: str = "",
                          topic: str = "",
                          record_application: bool = False
                          ) -> list[dict]:
    """Return active blindspots whose patterns match the given
    (model, topic). Records application counts when record_application
    is True (caller should set this when actually surfacing the
    caveat to the user, not when speculatively previewing).
    """
    out: list[dict] = []
    for bs in db.list_blindspots(active_only=True, limit=500):
        if not _matches_model(bs.get("model_pattern") or "", model):
            continue
        if not _matches_topic(bs.get("topic_pattern") or "", topic):
            continue
        out.append(bs)
        if record_application:
            try:
                db.record_blindspot_application(bs["id"])
            except Exception as e:
                log.warning("record_blindspot_application failed for "
                            "id=%s: %s", bs.get("id"), e)
    return out


def adjusted_confidence(reported: str, blindspots: list[dict]) -> str:
    """Apply the cumulative confidence_adjustment from a list of
    matching blindspots to a reported confidence label, returning the
    adjusted label. Negative adjustments downgrade; positive upgrade.

    Levels: low < med < high. Bounds clamp.
    """
    levels = ["low", "med", "high"]
    try:
        idx = levels.index((reported or "med").lower())
    except ValueError:
        idx = 1
    adj = sum(int(bs.get("confidence_adjustment") or 0)
              for bs in blindspots)
    new_idx = max(0, min(2, idx + adj))
    return levels[new_idx]


def format_caveat_block(blindspots: list[dict],
                        *, max_chars: int = 1200) -> str:
    """Render a list of blindspots as a caveats block for chat or
    working memory display."""
    if not blindspots:
        return ""
    lines = ["## Known blindspots applying to this view"]
    for bs in blindspots[:8]:
        body = (bs.get("body") or "").strip()
        if not body:
            continue
        adj = int(bs.get("confidence_adjustment") or 0)
        adj_note = ""
        if adj != 0:
            adj_note = " (treat reported confidence as {} {} level)".format(
                "+1" if adj > 0 else "-1",
                "higher" if adj > 0 else "lower",
            )
        conf = bs.get("confidence") or "med"
        lines.append("  - [{conf}{adj}] {body}".format(
            conf=conf, adj=adj_note, body=body,
        ))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + " …"
    return text


def topic_for_artifact(artifact: dict | None,
                       extra_keywords: list[str] | None = None) -> str:
    """Best-effort topic string for an artifact, used for blindspot
    matching. Pulls together title, body, tags, etc. into one
    haystack."""
    if not artifact:
        return " ".join(extra_keywords or [])
    parts = []
    for k in ("title", "question", "name", "body", "content",
              "summary", "text"):
        v = artifact.get(k)
        if v:
            parts.append(str(v))
    tags = artifact.get("tags")
    if tags:
        if isinstance(tags, list):
            parts.append(" ".join(str(t) for t in tags))
        else:
            parts.append(str(tags))
    if extra_keywords:
        parts.extend(extra_keywords)
    return " | ".join(parts)
