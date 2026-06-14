"""Taxonomy reprocess — sample-test the axis-aware gist prompt (2026-06-13).

The $18 question: re-gisting the whole corpus with a prompt that ALSO emits
the Lens + Modality axes is only worth paying for if re-reading the RAW
transcript recovers interpretive (lens) signal that the existing gist body
lost. (The current gist prompt optimizes for "THE CHANGE" = operational, so
the hypothesis is the lens signal was discarded at gist-generation time.)

This runs the axis-aware prompt TWO ways on a spread of real gists:
  A) over the RAW transcript   (the expensive full-corpus path, ~$0.006/gist)
  B) over the existing GIST body (the cheap path, ~$0.001/gist)
and prints both, plus real per-call cost, so we can judge whether A buys
enough lens signal over B to justify the spend.

Run ON the Pi (needs the OpenRouter key + the raw .jsonl files):
    sudo python3 /home/turfptax/cortex-core/scripts/taxonomy_sample_test.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
SECRETS = "/home/turfptax/.cortex/secrets.toml"
PLUGIN = "/home/turfptax/cortex-core/plugins/overseer"
MODEL = "anthropic/claude-sonnet-4.6"
# Sonnet pricing per Mtok (input / output).
PRICE_IN, PRICE_OUT = 3.0, 15.0
N_PER_SOURCE = 2  # gists per source bucket

sys.path.insert(0, PLUGIN)
from claude_jsonl import build_transcript_for_summary, parse_claude_code_jsonl

LENSES = ("continuity-of-self", "truth-under-distortion", "cover-story-vs-spine",
          "dialectic", "reciprocity", "making-hidden-visible")

AXIS_PROMPT = """You are re-summarizing one imported session into a GIST plus \
two interpretive axes. Output EXACTLY three lines, nothing else:

GIST: <one sentence capturing THE CHANGE - what shifted for the user that they \
didn't have before. Drop what they already knew. Describe what shifted for the \
human, not what the assistant did.>
MODALITY: <the epistemic kind of this gist - exactly one of: observation, \
statement, inference, hypothesis, value-judgment, external-claim, pattern>
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
        sys.exit("openrouter key not found")
    return m.group(1)


def call(key, content):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user",
                      "content": AXIS_PROMPT.format(
                          lenses=", ".join(LENSES), content=content[:14000])}],
        "max_tokens": 220, "temperature": 0.3,
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


def parse(text):
    out = {"gist": "", "modality": "", "lens": ""}
    for line in text.splitlines():
        for k in out:
            if line.upper().startswith(k.upper() + ":"):
                out[k] = line.split(":", 1)[1].strip()
    return out


def pick_sample(db):
    """A spread across source buckets, only gists whose raw .jsonl exists."""
    rows = db.execute(
        "SELECT g.id gid, g.body, s.source_path, "
        "  (SELECT t.tag FROM tags t WHERE t.table_name='summaries_gist' "
        "   AND t.row_id=g.id AND t.tag LIKE 'source:%' LIMIT 1) src "
        "FROM summaries_gist g "
        "JOIN processed_imported_sessions p ON p.gist_id=g.id "
        "JOIN imported_sessions s ON s.id=p.imported_id "
        "ORDER BY g.id DESC").fetchall()
    import os
    buckets, sample = {}, []
    for r in rows:
        if not (r["source_path"] and os.path.isfile(r["source_path"])):
            continue
        b = (r["src"] or "source:unknown").split(":")[1]
        buckets.setdefault(b, [])
        if len(buckets[b]) < N_PER_SOURCE:
            buckets[b].append(r)
            sample.append(r)
        if len(sample) >= 12:
            break
    return sample


def main():
    key = load_key()
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    sample = pick_sample(db)
    print("sampling {} gists across sources\n".format(len(sample)))
    a_total = b_total = 0.0
    lens_a = lens_b = 0
    done = 0
    for r in sample:
        try:
            meta, msgs = parse_claude_code_jsonl(Path(r["source_path"]))
            transcript, _ = build_transcript_for_summary(msgs, max_chars=12000)
        except Exception as e:
            print("g:{} raw parse failed: {}".format(r["gid"], e))
            continue
        a_text, a_cost = call(key, transcript)
        b_text, b_cost = call(key, "EXISTING GIST: " + (r["body"] or ""))
        a_total += a_cost
        b_total += b_cost
        done += 1
        a, b = parse(a_text), parse(b_text)
        if a["lens"] and a["lens"].lower() != "none":
            lens_a += 1
        if b["lens"] and b["lens"].lower() != "none":
            lens_b += 1
        print("=" * 70)
        print("g:{}  cost A=${:.4f} B=${:.4f}".format(
            r["gid"], a_cost, b_cost))
        print("  OLD gist: {}".format((r["body"] or "")[:120]))
        print("  [A raw]  mod={} | lens={}".format(a["modality"], a["lens"]))
        print("           gist: {}".format(a["gist"][:120]))
        print("  [B body] mod={} | lens={}".format(b["modality"], b["lens"]))
    print("\n" + "=" * 70)
    print("SAMPLE: {} gists | spent A=${:.3f} B=${:.3f} total=${:.3f}".format(
        done, a_total, b_total, a_total + b_total))
    if done:
        per_a = a_total / done
        per_b = b_total / done
        print("avg cost/gist:  A(raw)=${:.4f}  B(gist-body)=${:.4f}".format(
            per_a, per_b))
        print("EXTRAPOLATED full run over 2951 raw-backed gists:")
        print("   path A (raw reprocess): ~${:.2f}".format(per_a * 2951))
        print("   path B (gist-body only): ~${:.2f}".format(per_b * 3687))
        print("lens found: A(raw)={}/{}  B(gist-body)={}/{}".format(
            lens_a, done, lens_b, done))
        print("--> if A finds materially more lens than B, the raw reprocess "
              "earns the spend; if not, the cheap gist-body pass suffices.")


if __name__ == "__main__":
    main()
