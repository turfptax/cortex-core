"""Taxonomy reprocess - stamp the Modality + Lens axes onto every gist.

Reads each gist's RAW transcript (the gist body optimizes for THE CHANGE and
discarded the lens signal at generation time - see taxonomy_sample_test.py)
and writes back two axes:
  - modality  : the integrity-pair epistemic kind (observation, statement, ...)
  - lens      : zero+ of the 6 controlled interpretive lenses, or "none"
The curated gist BODY is left untouched (axes-only, reversible). Resumable:
only rows with axis_processed_at IS NULL are touched, so an interrupted or
budget-capped run picks up where it stopped.

SAFE BY DEFAULT - a bare run is a dry run (reports scope + projected cost,
writes nothing). A live run must pass BOTH --go and --max-cost <ceiling>; the
run aborts the moment spend would cross the ceiling.

    # dry run - scope + projected cost, no spend, no writes:
    sudo python3 /home/turfptax/cortex-core/scripts/taxonomy_reprocess.py

    # live run, hard-capped at $18, this invocation does up to 4000 gists:
    sudo python3 /home/turfptax/cortex-core/scripts/taxonomy_reprocess.py \
        --go --max-cost 18 --limit 4000
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
SECRETS = "/home/turfptax/.cortex/secrets.toml"
PLUGIN = "/home/turfptax/cortex-core/plugins/overseer"
MODEL = "anthropic/claude-sonnet-4.6"
PRICE_IN, PRICE_OUT = 3.0, 15.0  # Sonnet per Mtok (in / out)
AXIS_PROMPT_VERSION = "axis-v1-2026-06-13"

sys.path.insert(0, PLUGIN)
from claude_jsonl import build_transcript_for_summary, parse_claude_code_jsonl

LENSES = ("continuity-of-self", "truth-under-distortion", "cover-story-vs-spine",
          "dialectic", "reciprocity", "making-hidden-visible")
MODALITIES = ("observation", "statement", "inference", "hypothesis",
              "value-judgment", "external-claim", "pattern")

AXIS_PROMPT = """You are stamping two interpretive axes onto one summarized \
session. Output EXACTLY two lines, nothing else:

MODALITY: <the epistemic kind of what shifted for the user - exactly one of: \
{modalities}>
LENS: <zero or more of the user's interpretive lenses this session GENUINELY \
illuminates, comma-separated, or "none". Be conservative - most operational \
sessions illuminate NO lens; forcing a connection corrupts the signal. Choose \
ONLY from: {lenses}>

CONTENT:
{content}
"""


def load_key():
    txt = Path(SECRETS).read_text(encoding="utf-8")
    m = re.search(r'\[openrouter\][^\[]*?key\s*=\s*"([^"]+)"', txt, re.DOTALL)
    if not m:
        sys.exit("openrouter key not found in " + SECRETS)
    return m.group(1)


def call(key, content, max_chars):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": AXIS_PROMPT.format(
            modalities=", ".join(MODALITIES), lenses=", ".join(LENSES),
            content=content[:max_chars])}],
        "max_tokens": 120, "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read())
    text = d["choices"][0]["message"]["content"]
    u = d.get("usage") or {}
    cost = (u.get("prompt_tokens", 0) * PRICE_IN
            + u.get("completion_tokens", 0) * PRICE_OUT) / 1_000_000
    return text.strip(), cost


def parse_axes(text):
    """Pull MODALITY + LENS, validated against the controlled vocabularies.
    Unknown modality -> None (leave unstamped, re-runnable). Unknown lens
    values are dropped; empty -> 'none'."""
    modality, lens_raw = None, ""
    for line in text.splitlines():
        up = line.upper()
        if up.startswith("MODALITY:"):
            v = line.split(":", 1)[1].strip().lower()
            modality = v if v in MODALITIES else None
        elif up.startswith("LENS:"):
            lens_raw = line.split(":", 1)[1].strip()
    if lens_raw.lower() in ("", "none"):
        lens = "none"
    else:
        keep = [p.strip().lower() for p in lens_raw.split(",")
                if p.strip().lower() in LENSES]
        lens = ", ".join(keep) if keep else "none"
    return modality, lens


def pending(db):
    """Gists not yet axis-stamped, with their raw source_path if any.
    Raw resolves via the imported-session JOIN (the 80% with .jsonl);
    the rest fall back to the gist body."""
    return db.execute(
        "SELECT g.id gid, g.body, "
        "  (SELECT s.source_path FROM processed_imported_sessions p "
        "   JOIN imported_sessions s ON s.id = p.imported_id "
        "   WHERE p.gist_id = g.id LIMIT 1) source_path "
        "FROM summaries_gist g "
        "WHERE g.axis_processed_at IS NULL "
        "ORDER BY g.id DESC").fetchall()


def content_for(row, max_chars):
    """Raw transcript if the .jsonl is present, else the gist body."""
    sp = row["source_path"]
    if sp and Path(sp).is_file():
        try:
            _, msgs = parse_claude_code_jsonl(Path(sp))
            transcript, _ = build_transcript_for_summary(msgs, max_chars=max_chars)
            if transcript.strip():
                return transcript, "raw"
        except Exception:
            pass
    return "EXISTING GIST: " + (row["body"] or ""), "body"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true",
                    help="actually spend + write (default: dry run)")
    ap.add_argument("--max-cost", type=float, default=0.0,
                    help="hard $ ceiling; required with --go, run aborts before crossing it")
    ap.add_argument("--limit", type=int, default=0,
                    help="max gists this invocation (0 = no limit)")
    ap.add_argument("--max-chars", type=int, default=12000,
                    help="raw-transcript cap (cost knob; 12000 validated)")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = pending(db)
    raw_n = sum(1 for r in rows if r["source_path"]
                and Path(r["source_path"]).is_file())
    body_n = len(rows) - raw_n
    # Per-gist rates measured by taxonomy_sample_test.py at max_chars=12000.
    est = raw_n * 0.0047 + body_n * 0.0016
    print("pending (axis_processed_at IS NULL): {}  (raw {} / body-only {})"
          .format(len(rows), raw_n, body_n))
    print("projected cost over ALL pending @ {}-char cap: ~${:.2f}"
          .format(args.max_chars, est))

    if not args.go:
        print("\nDRY RUN - nothing written, nothing spent.")
        print("To run live: --go --max-cost <ceiling> [--limit N]")
        return
    if args.max_cost <= 0:
        sys.exit("--go requires --max-cost <ceiling> (a hard $ safety bound)")

    key = load_key()
    todo = rows if not args.limit else rows[:args.limit]
    spent = 0.0
    done = 0
    mod_dist, lens_hits = {}, 0
    t0 = time.time()
    for r in todo:
        if spent >= args.max_cost:
            print("\n[STOP] cost ceiling ${:.2f} reached - aborting cleanly."
                  .format(args.max_cost))
            break
        content, kind = content_for(r, args.max_chars)
        try:
            text, cost = call(key, content, args.max_chars)
        except Exception as e:
            print("g:{} call failed ({}) - left unstamped".format(r["gid"], e))
            continue
        spent += cost
        modality, lens = parse_axes(text)
        if modality is None:
            print("g:{} unparseable modality - left unstamped".format(r["gid"]))
            continue
        db.execute(
            "UPDATE summaries_gist SET modality=?, lens=?, "
            "axis_processed_at=datetime('now') WHERE id=?",
            (modality, lens, r["gid"]))
        done += 1
        mod_dist[modality] = mod_dist.get(modality, 0) + 1
        if lens != "none":
            lens_hits += 1
        if done % 25 == 0:
            db.commit()
            print("  ...{} stamped | ${:.3f} | {:.0f}s | last g:{} [{}] {} | {}"
                  .format(done, spent, time.time() - t0, r["gid"], kind,
                          modality, lens))
    db.commit()
    print("\n" + "=" * 70)
    print("DONE: stamped {} gists | spent ${:.3f}".format(done, spent))
    print("modality distribution:", dict(sorted(
        mod_dist.items(), key=lambda kv: -kv[1])))
    print("lens hits: {}/{}  ({:.0f}% touched a lens)".format(
        lens_hits, done, 100 * lens_hits / done if done else 0))
    remaining = len(rows) - done
    if remaining > 0:
        print("remaining pending: {} - re-run with --go to continue".format(
            remaining))


if __name__ == "__main__":
    main()
