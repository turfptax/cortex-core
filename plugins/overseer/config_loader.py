"""Instance config loader (2026-06-23).

Loads instance-specific personalization (sensitivity/category rules, instance
settings, ingest lists) from a GITIGNORED local config so the public repo ships
generic. Real values never live in tracked code; this is the segmentation that
prevents the 2026-06-22 leak class from recurring.

Precedence (first existing wins for the local file):
  1. $CORTEX_LOCAL_CONFIG
  2. ~/.cortex/cortex.local.toml  (+ the root / Pi / etc equivalents)
The committed data/cortex.example.toml supplies generic, fail-CLOSED defaults
(no real sensitivity rules -> unconfigured installs gist-and-drop, never raw).
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_EXAMPLE = _HERE / "data" / "cortex.example.toml"

# Mirrors the secrets_paths ergonomics in plugin.toml. First existing wins.
_CANDIDATES = [
    os.environ.get("CORTEX_LOCAL_CONFIG"),
    os.path.expanduser("~/.cortex/cortex.local.toml"),
    "/home/turfptax/.cortex/cortex.local.toml",
    "/root/.cortex/cortex.local.toml",
    "/etc/cortex/cortex.local.toml",
]

_cache = None


def _read(path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _local_path():
    for p in _CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def load(force: bool = False) -> dict:
    """Merged config: generic example defaults overlaid with the local file
    (local sections win, including the list-sections). Cached."""
    global _cache
    if _cache is not None and not force:
        return _cache
    cfg = _read(_EXAMPLE)
    lp = _local_path()
    if lp:
        for k, v in _read(lp).items():
            cfg[k] = v
        cfg["_source"] = lp
        cfg["_has_local"] = True
    else:
        cfg["_source"] = f"{_EXAMPLE} (no local config; fail-closed defaults)"
        cfg["_has_local"] = False
    _cache = cfg
    return cfg


def source() -> str:
    return load().get("_source", "")


def has_local_config() -> bool:
    return bool(load().get("_has_local"))


def instance() -> dict:
    return load().get("instance", {}) or {}


def git_ingest() -> dict:
    return load().get("git_ingest", {}) or {}


def tiers() -> dict:
    return load().get("tiers", {}) or {}


def off_box_max_tier(default: str = "internal") -> str:
    return (tiers().get("off_box_max") or default).strip() or default


def sensitivity_seeds() -> list:
    """[{match, pattern, tier, retention, priority?, note?}]. Empty => fail-closed."""
    return load().get("sensitivity", []) or []


def category_rules() -> list:
    """[{kind, pattern, category}] for resolve_category()."""
    return load().get("category", []) or []


def owner_name(default: str = "the user") -> str:
    return (instance().get("owner_name") or "").strip() or default
