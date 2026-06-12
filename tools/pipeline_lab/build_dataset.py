"""Build a study dataset for Pipeline Lab: 5 curated + N random sessions.

Scans every Claude Code .jsonl under ~/.claude/projects, computes stats with
the production parser, then selects:

  CURATED (5), by explicit rules:
    1. longest session by duration
    2. densest dialogue (most user messages)
    3. earliest substantial session (time diversity)
    4. best session from a project OTHER than the two giants
       (Cortex / UFOSINT), so the set is not monoculture
    5. highest human-share of messages (user/(user+assistant)) among
       sessions with 30+ messages

  RANDOM (default 20): seeded sample from the remaining eligible pool,
  so the same command regenerates the same dataset.

Eligibility floor for both lists: at least 5 user messages (filters
empty/tool-spam sessions that would waste study time).

Output: tools/pipeline_lab/dataset.json (gitignored: contains personal
paths). Pipeline Lab's UI picks it up automatically.

Run:  python tools/pipeline_lab/build_dataset.py [--random-n 20] [--seed 42]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "plugins" / "overseer"))

from claude_jsonl import parse_claude_code_jsonl  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"
# Non-Claude sources (ChatGPT, Grok, ...): the Pi importers normalize them to
# the same .jsonl shape; copy samples from the Pi's
# plugins/overseer/data/imports/ into this dir as <source>__<id>.jsonl.
SAMPLES_DIR = HERE / "imported_samples"
MIN_USER_MSGS = 5
GIANTS = ("C--dev-ttx-Cortex", "C--dev-dg-UFOSINT")


def scan() -> list[dict]:
    rows = []
    files = sorted(PROJECTS_DIR.glob("*/*.jsonl"))
    print(f"scanning {len(files)} session files...")
    for i, f in enumerate(files):
        if i and i % 50 == 0:
            print(f"  {i}/{len(files)}")
        try:
            meta, _messages = parse_claude_code_jsonl(str(f))
        except Exception as e:
            print(f"  skip {f.name}: {e}")
            continue
        u = int(meta.get("user_message_count") or 0)
        a = int(meta.get("assistant_message_count") or 0)
        rows.append({
            "path": str(f).replace("\\", "/"),
            "project": f.parent.name,
            "file": f.name,
            "size_kb": round(f.stat().st_size / 1024),
            "started_at": meta.get("started_at") or "",
            "duration_minutes": int(meta.get("duration_minutes") or 0),
            "messages": int(meta.get("message_count") or 0),
            "user_messages": u,
            "assistant_messages": a,
            "human_share": round(u / (u + a), 3) if (u + a) else 0.0,
        })
    return rows


def pick_curated(pool: list[dict]) -> list[dict]:
    picked: list[dict] = []

    def take(row, why):
        if row and not any(p["path"] == row["path"] for p in picked):
            row = dict(row)
            row["why"] = why
            picked.append(row)

    by_duration = sorted(pool, key=lambda r: -r["duration_minutes"])
    by_user = sorted(pool, key=lambda r: -r["user_messages"])
    by_date = sorted((r for r in pool if r["started_at"]),
                     key=lambda r: r["started_at"])
    non_giant = [r for r in by_user if r["project"] not in GIANTS]
    human_heavy = sorted((r for r in pool if r["messages"] >= 30),
                         key=lambda r: -r["human_share"])

    take(next(iter(by_duration), None),
         "longest session by duration: the head/tail truncation stress test")
    take(next(iter(by_user), None),
         "densest dialogue (most user messages): can one line hold this?")
    take(next(iter(by_date), None),
         "earliest substantial session: does the gist frame fit old work?")
    take(next(iter(non_giant), None),
         "best session outside the two giant projects: source diversity")
    take(next(iter(human_heavy), None),
         "highest human share of messages: conversation, not tool traffic")

    # Backfill from runners-up if rules collided on the same file.
    for r in by_duration:
        if len(picked) >= 5:
            break
        take(r, "runner-up by duration (backfill after rule collision)")
    return picked[:5]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--random-n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rows = scan()
    eligible = [r for r in rows if r["user_messages"] >= MIN_USER_MSGS]
    print(f"{len(rows)} parsed, {len(eligible)} eligible (>= {MIN_USER_MSGS} user messages)")

    curated = pick_curated(eligible)
    curated_paths = {r["path"] for r in curated}
    rest = [r for r in eligible if r["path"] not in curated_paths]
    rng = random.Random(args.seed)
    randoms = rng.sample(rest, min(args.random_n, len(rest)))
    randoms.sort(key=lambda r: r["started_at"])

    imported = []
    for f in sorted(SAMPLES_DIR.glob("*.jsonl")) if SAMPLES_DIR.is_dir() else []:
        try:
            meta, _ = parse_claude_code_jsonl(str(f))
        except Exception as e:
            print(f"  skip sample {f.name}: {e}")
            continue
        u = int(meta.get("user_message_count") or 0)
        a = int(meta.get("assistant_message_count") or 0)
        imported.append({
            "path": str(f).replace("\\", "/"),
            "project": f.name.split("__")[0],
            "file": f.name,
            "size_kb": round(f.stat().st_size / 1024),
            "started_at": meta.get("started_at") or "",
            "duration_minutes": int(meta.get("duration_minutes") or 0),
            "messages": int(meta.get("message_count") or 0),
            "user_messages": u,
            "assistant_messages": a,
            "human_share": round(u / (u + a), 3) if (u + a) else 0.0,
        })

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed,
        "eligibility": f">= {MIN_USER_MSGS} user messages",
        "scanned": len(rows),
        "eligible": len(eligible),
        "curated": curated,
        "random": randoms,
        "imported": imported,
    }
    dest = HERE / "dataset.json"
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nwrote {dest}")
    print("\nCURATED:")
    for r in curated:
        print(f"  {r['duration_minutes']:>4}min {r['user_messages']:>4}u "
              f"{r['project'][:36]:<36} {r['why']}")
    print(f"\nRANDOM: {len(randoms)} sessions, seed {args.seed}")
    print(f"IMPORTED SAMPLES: {len(imported)} (from {SAMPLES_DIR.name}/)")


if __name__ == "__main__":
    main()
