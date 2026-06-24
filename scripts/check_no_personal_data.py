#!/usr/bin/env python3
"""Pre-commit / CI guardrail: block personal data + secrets from being committed.

Two layers:
  1. Pattern layer (ALWAYS, including public CI): refuse files that must never be
     tracked (cortex.local.toml / *.local.toml / secrets.toml / *.db) and refuse
     content matching secret patterns (API keys, tokens, private keys).
  2. Name layer (ONLY when ~/.cortex/cortex.local.toml provides [linter].deny_names):
     refuse content containing your real confidential names. The names live ONLY
     in the gitignored config, so they never ship in this script and never appear
     in public CI logs (per the privacy policy). Findings name the file, not the
     matched string.

Exit non-zero on any finding. Pass file paths as argv, or none to scan the
git staged set.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None

FORBIDDEN_PATH = re.compile(
    r"(^|/)(cortex\.local\.toml|secrets\.toml|[^/]*\.local\.toml)$"
    r"|\.(db|sqlite)(-shm|-wal)?$",
    re.I,
)

SECRET_PATTERNS = [
    (re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"), "OpenRouter key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{30,}"), "GitHub PAT"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]


def _deny_names() -> list:
    p = os.path.expanduser("~/.cortex/cortex.local.toml")
    if tomllib is None or not os.path.exists(p):
        return []
    try:
        with open(p, "rb") as f:
            cfg = tomllib.load(f)
        return [n for n in (cfg.get("linter", {}).get("deny_names", []) or []) if n]
    except Exception:
        return []


def _staged() -> list:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True,
    ).stdout
    return [l for l in out.splitlines() if l]


def main() -> int:
    files = sys.argv[1:] or _staged()
    names = _deny_names()
    name_res = [re.compile(re.escape(n), re.I) for n in names]
    findings = []
    for f in files:
        if FORBIDDEN_PATH.search(f):
            findings.append(f"{f}: forbidden path (personal/secret file must stay gitignored)")
            continue
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            continue
        for rx, label in SECRET_PATTERNS:
            if rx.search(text):
                findings.append(f"{f}: contains a {label}")
        for rx in name_res:
            if rx.search(text):
                findings.append(f"{f}: contains a confidential name from your local deny list")
                break
    if findings:
        sys.stderr.write(
            "PERSONAL-DATA GUARDRAIL: commit blocked\n"
            + "\n".join("  - " + x for x in findings) + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
