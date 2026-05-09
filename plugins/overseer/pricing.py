"""Anthropic API pricing — used by project_summary to estimate
session costs from token counts.

PRICING IS HARDCODED. These rates reflect Anthropic's published
prices as_of 2026-05-02. If rates change, update PRICE_TABLE below.
The structure is dollars per MILLION tokens. Cache write rate is
1.25x the base input rate; cache read rate is 0.1x. These multipliers
are the standard Anthropic prompt-caching tiers.

If a model isn't in the table (e.g. an LLM-side model id we haven't
seen), `estimate_cost` returns 0 instead of fabricating a number.
The caller can detect zero costs in the project_summaries metadata
and either ignore them or flag for manual price entry.

Slice 4 CP1a — Tory's call: hardcode with as_of comment, easy to
update later (vs reading from a config file).
"""

from __future__ import annotations

import logging
import re


log = logging.getLogger("plugin.overseer.pricing")


# Dollars per 1,000,000 tokens. {model_match: (input_per_m, output_per_m)}
# Match keys are substrings (lowercased) tested against the model id.
# First match wins, so order matters — list specific variants before
# family fallbacks.
#
# All prices as_of 2026-05-02.
PRICE_TABLE: list[tuple[str, tuple[float, float]]] = [
    # Opus family
    ("claude-opus-4-7",    (15.0, 75.0)),
    ("claude-opus-4-6",    (15.0, 75.0)),
    ("claude-opus-4-5",    (15.0, 75.0)),
    ("claude-opus-4",      (15.0, 75.0)),
    ("claude-3-opus",      (15.0, 75.0)),
    ("opus",               (15.0, 75.0)),

    # Sonnet family
    ("claude-sonnet-4-6",  (3.0,  15.0)),
    ("claude-sonnet-4-5",  (3.0,  15.0)),
    ("claude-sonnet-4",    (3.0,  15.0)),
    ("claude-3-7-sonnet",  (3.0,  15.0)),
    ("claude-3-5-sonnet",  (3.0,  15.0)),
    ("claude-3-sonnet",    (3.0,  15.0)),
    ("sonnet",             (3.0,  15.0)),

    # Haiku family
    ("claude-haiku-4-5",   (1.0,  5.0)),
    ("claude-haiku-4",     (1.0,  5.0)),
    ("claude-3-5-haiku",   (0.8,  4.0)),
    ("claude-3-haiku",     (0.25, 1.25)),
    ("haiku",              (1.0,  5.0)),
]

CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def _lookup(model: str) -> tuple[float, float] | None:
    """Return (input_per_m, output_per_m) for a model id, or None
    if the model isn't priced. Substring match — handles dated
    suffixes like '-20260315' transparently."""
    if not model:
        return None
    m = model.lower()
    for key, rates in PRICE_TABLE:
        if key in m:
            return rates
    return None


def estimate_cost(model: str, usage: dict) -> float:
    """Cost in USD for a single assistant turn.

    `usage` shape (Anthropic API):
      {
        "input_tokens": int,
        "output_tokens": int,
        "cache_creation_input_tokens": int,  # optional
        "cache_read_input_tokens":     int,  # optional
      }

    Returns 0.0 if model is unknown — caller can decide whether to
    surface that as a "rates unavailable" caveat.
    """
    rates = _lookup(model or "")
    if rates is None:
        return 0.0
    input_per_m, output_per_m = rates
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cc_tok = int(usage.get("cache_creation_input_tokens") or 0)
    cr_tok = int(usage.get("cache_read_input_tokens") or 0)
    cost = (
        in_tok * input_per_m / 1_000_000
        + out_tok * output_per_m / 1_000_000
        + cc_tok * input_per_m * CACHE_WRITE_MULTIPLIER / 1_000_000
        + cr_tok * input_per_m * CACHE_READ_MULTIPLIER / 1_000_000
    )
    return cost


def estimate_cost_from_totals(
    *,
    models_used: dict,
    tokens_input_total: int,
    tokens_output_total: int,
    tokens_cache_creation_total: int,
    tokens_cache_read_total: int,
) -> tuple[float, bool]:
    """Estimate cost across an aggregate that mixes models. We don't
    have per-model token splits at the aggregate level — best we can
    do is weight by message-count share of each model.

    Returns (cost_usd, has_unknown_model). `has_unknown_model` flags
    when at least one model in the mix isn't priced, so the caller
    knows the cost is a lower bound.

    For sessions where all assistant turns came from one model the
    estimate is exact. For mixed-model sessions it's approximate.
    """
    if not models_used:
        return (0.0, False)
    total_msgs = sum(int(v) for v in models_used.values()) or 1
    cost = 0.0
    has_unknown = False
    for model, count in models_used.items():
        rates = _lookup(model or "")
        if rates is None:
            has_unknown = True
            continue
        share = count / total_msgs
        input_per_m, output_per_m = rates
        cost += (
            tokens_input_total * share * input_per_m / 1_000_000
            + tokens_output_total * share * output_per_m / 1_000_000
            + tokens_cache_creation_total * share * input_per_m
              * CACHE_WRITE_MULTIPLIER / 1_000_000
            + tokens_cache_read_total * share * input_per_m
              * CACHE_READ_MULTIPLIER / 1_000_000
        )
    return (cost, has_unknown)


def is_known_model(model: str) -> bool:
    """True if `model` matches a row in PRICE_TABLE — for callers
    that want to validate input before estimating."""
    return _lookup(model or "") is not None


# Optional: simple model-family classifier for charting / display.
_FAMILY_RX = re.compile(
    r"\b(opus|sonnet|haiku)\b", re.IGNORECASE,
)


def model_family(model: str) -> str:
    """Return 'opus' | 'sonnet' | 'haiku' | 'other'."""
    if not model:
        return "other"
    m = _FAMILY_RX.search(model.lower())
    return m.group(1) if m else "other"
