#!/usr/bin/env python3
"""Backlog pass: classify the NATURE of every imported session.

Runs the Stage 0.5 session classifier (plugins/overseer/session_classifier.py,
human-audited 2026-06-11) over all imported_sessions rows that have a raw
source file, and persists results to a new, non-invasive table:

    session_nature(session_id PK, source, category, confidence, margin,
                   method, weight, treatment, ambiguous, signals_json,
                   classified_at, classified_at_local)

This is DATA ONLY. Nothing in the loop reads this table yet; wiring the
pipeline to act on categories (differentiated treatment per the locked
2026-06-11 vision) is a separate, later step.

Run on the Pi (.25):
    cd /home/turfptax/cortex-core
    sudo CORTEX_SECRETS=/home/turfptax/.cortex/secrets.toml \
        python3 -u scripts/classify_backlog.py --llm

Flags:
    --llm      escalate rule-ambiguous sessions to one Flash call each
               (audit showed ~0 percent need this; cost is pennies)
    --force    reclassify rows already in session_nature
    --limit N  stop after N sessions (smoke testing)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "plugins" / "overseer"))

from claude_jsonl import parse_claude_code_jsonl  # noqa: E402
from session_classifier import classify_session  # noqa: E402

OVERSEER_DB = REPO / "plugins" / "overseer" / "data" / "overseer.db"

DDL = """CREATE TABLE IF NOT EXISTS session_nature (
  session_id TEXT PRIMARY KEY,
  source TEXT,
  category TEXT NOT NULL,
  confidence REAL,
  margin REAL,
  method TEXT,
  weight REAL,
  treatment TEXT,
  ambiguous INTEGER DEFAULT 0,
  signals_json TEXT,
  classified_at TEXT NOT NULL,
  classified_at_local TEXT
)"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    llm = None
    if args.llm:
        import tomllib
        from llm_router import LLMRouter
        with open(REPO / "plugins" / "overseer" / "plugin.toml", "rb") as f:
            manifest = tomllib.load(f)
        llm = LLMRouter(manifest_llm=manifest.get("llm", {}), db=None)

    con = sqlite3.connect(str(OVERSEER_DB), timeout=15)
    con.execute(DDL)
    con.commit()

    rows = con.execute(
        "SELECT id, source, source_path FROM imported_sessions "
        "WHERE source_path IS NOT NULL AND source_path != ''").fetchall()
    done = set()
    if not args.force:
        done = {r[0] for r in con.execute(
            "SELECT session_id FROM session_nature")}

    todo = [r for r in rows if r[0] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"imported_sessions with files: {len(rows)}; to classify: {len(todo)}")

    counts: dict[str, int] = {}
    ambiguous = errors = llm_calls = 0
    cost = 0.0
    t0 = time.time()
    for i, (sid, source, src_path) in enumerate(todo, 1):
        try:
            meta, msgs = parse_claude_code_jsonl(src_path)
            meta["source"] = source or "claude-code"
            r = classify_session(meta, msgs, llm=llm)
            if r.get("llm"):
                llm_calls += 1
                cost += (r["llm"].get("cost_usd") or 0)
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            now_local = datetime.now().astimezone().isoformat(timespec="seconds")
            con.execute(
                "INSERT OR REPLACE INTO session_nature (session_id, source, "
                "category, confidence, margin, method, weight, treatment, "
                "ambiguous, signals_json, classified_at, classified_at_local) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, source, r["category"], r["confidence"], r["margin"],
                 r["method"], r["weight"], r["treatment"],
                 1 if r["ambiguous"] else 0, json.dumps(r["signals"]),
                 now_utc, now_local))
            counts[r["category"]] = counts.get(r["category"], 0) + 1
            if r["ambiguous"]:
                ambiguous += 1
        except Exception as e:
            errors += 1
            print(f"  ERROR {sid}: {str(e)[:120]}")
        if i % 50 == 0:
            con.commit()
        if i % 200 == 0:
            rate = i / (time.time() - t0)
            print(f"  {i}/{len(todo)} ({rate:.1f}/s) {counts}")
    con.commit()

    print("\nDONE", json.dumps({
        "classified": len(todo) - errors, "errors": errors,
        "distribution": counts, "still_ambiguous": ambiguous,
        "llm_calls": llm_calls, "llm_cost_usd": round(cost, 4),
        "elapsed_s": round(time.time() - t0, 1),
    }))
    con.close()


if __name__ == "__main__":
    main()
