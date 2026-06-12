"""Pipeline Lab: a standalone inspection harness for raw -> gist -> abstraction.

Runs the PRODUCTION pipeline code (same prompt builders, same parser, same
LLM router and model overrides from plugin.toml) against a pasted
conversation or a local Claude Code .jsonl, and shows every intermediate:
the rendered prompts, the raw model responses, the parsed structures, and
per-stage cost. Nothing here writes to overseer.db or cortex.db.

Run the web UI:
    python tools/pipeline_lab/pipeline_lab.py
    -> http://localhost:8777

Run from the command line (results print AND save for the UI's
saved-run / compare dropdowns):
    python tools/pipeline_lab/pipeline_lab.py --run path/to/session.jsonl
    python tools/pipeline_lab/pipeline_lab.py --batch curated
    python tools/pipeline_lab/pipeline_lab.py --batch random
    python tools/pipeline_lab/pipeline_lab.py --batch all

Requirements:
    - Python 3.11+ (tomllib)
    - An OpenRouter key the router can find: OPENROUTER_API_KEY env var or
      ~/.cortex/secrets.toml with [openrouter] api_key = "..."
    - Optional: the Pi reachable at 10.0.0.25:8420 to fetch the live
      EXISTING themes/patterns/drift lists and active open questions, so
      insight-scan dedup and evidence routing behave exactly as production.

Companion doc: docs/PIPELINE.md (the human-readable spec of this logic).
"""

from __future__ import annotations

import base64
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
RUNS_DIR = HERE / "runs"

# Import the REAL pipeline modules. Fidelity is the whole point: if these
# imports break, fix the path, never copy the logic.
sys.path.insert(0, str(REPO / "plugins" / "overseer"))
sys.path.insert(0, str(REPO / "src"))

from claude_jsonl import build_transcript_for_summary, parse_claude_code_jsonl  # noqa: E402
from insight_scan import (  # noqa: E402
    SCAN_PROMPT_TEMPLATE, _format_existing, _format_gist_block, parse_scan_response,
)
from llm_router import LLMRouter, _get_openrouter_api_key, _load_secrets  # noqa: E402
from prompts import import_gist_prompt  # noqa: E402
from question_routing import build_routing_prompt, parse_routing_response  # noqa: E402
from session_classifier import classify_session  # noqa: E402

PORT = 8777
PI_BASE = "http://10.0.0.25:8420"
PI_AUTH = "Basic " + base64.b64encode(b"cortex:cortex").decode()

# Production call parameters, mirrored from loop.py / insight_scan.py /
# question_routing.py. If production changes, change these and PIPELINE.md.
GIST_PARAMS = {"purpose": "summarize-session", "max_tokens": 200, "temperature": 0.4}
INSIGHT_PARAMS = {"purpose": "insight-scan", "max_tokens": 1500, "temperature": 0.3}
ROUTING_PARAMS = {"purpose": "evidence-routing", "max_tokens": 400, "temperature": 0.3}


def load_manifest_llm() -> dict:
    with open(REPO / "plugins" / "overseer" / "plugin.toml", "rb") as f:
        manifest = tomllib.load(f)
    return manifest.get("llm", {})


def make_router() -> LLMRouter:
    return LLMRouter(manifest_llm=load_manifest_llm(), db=None)


def pi_get(path: str, timeout: float = 4.0):
    req = urllib.request.Request(PI_BASE + path, headers={"Authorization": PI_AUTH})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def first_list(payload) -> list:
    """Pi routes wrap their list under varying keys; take the first list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list):
                return v
    return []


def fetch_pi_context() -> dict:
    """Live EXISTING lists + active questions, or empty fallbacks."""
    ctx = {"reachable": False, "themes": [], "patterns": [], "drift": [], "questions": []}
    try:
        ctx["themes"] = first_list(pi_get("/plugins/overseer/themes"))
        ctx["patterns"] = first_list(pi_get("/plugins/overseer/patterns"))
        ctx["drift"] = first_list(pi_get("/plugins/overseer/drift"))
        ctx["questions"] = [
            q for q in first_list(pi_get("/plugins/overseer/questions"))
            if q.get("is_active", 1)
        ]
        ctx["reachable"] = True
    except Exception as e:
        ctx["error"] = str(e)
    return ctx


def synth_metadata(text: str) -> dict:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {
        "session_id": "pasted-" + datetime.now(timezone.utc).strftime("%H%M%S"),
        "started_at": "?", "ended_at": "?", "duration_minutes": 0,
        "message_count": len(lines), "user_message_count": 0,
        "assistant_message_count": 0,
    }


def run_pipeline(body: dict) -> dict:
    """Execute gist -> insight scan -> routing with full intermediates."""
    router = make_router()
    stages = []
    t0 = time.time()

    # ── Stage 0: raw input -> transcript ─────────────────────────
    mode = body.get("mode") or "text"
    project = (body.get("project") or "pipeline-lab").strip()
    if mode == "jsonl":
        path = (body.get("jsonl_path") or "").strip()
        meta, messages = parse_claude_code_jsonl(path)
        transcript, tstats = build_transcript_for_summary(messages)
        source_label = Path(path).name
    else:
        raw_text = body.get("text") or ""
        meta = synth_metadata(raw_text)
        transcript = raw_text[:30000]
        tstats = {
            "messages_total": meta["message_count"],
            "messages_used": meta["message_count"],
            "messages_omitted": 0,
            "strategy": "pasted-verbatim",
            "char_length": len(transcript),
        }
        source_label = "pasted text"
    stages.append({
        "name": "transcript", "label": "Stage 0: raw -> transcript",
        "parsed": {"metadata": meta, "stats": tstats},
        "note": "Head/tail truncation here is the first lossy step and happens before any model.",
    })

    # ── Stage 0.5: session NATURE classification (proposed) ──────
    if mode == "jsonl":
        cls_messages = messages
    else:
        cls_messages = [{"role": "user", "content_text": body.get("text") or "",
                         "has_tool_use": False}]
    cls = classify_session(meta, cls_messages, llm=router)
    llm_part = cls.get("llm") or {}
    stages.append({
        "name": "classify", "label": "Stage 0.5: session classification (proposed, Lab-only)",
        "prompt": llm_part.get("prompt"),
        "model": llm_part.get("model"), "cost_usd": llm_part.get("cost_usd"),
        "raw_response": llm_part.get("raw_response"),
        "parsed": {k: cls[k] for k in (
            "category", "confidence", "margin", "method", "weight",
            "treatment", "ambiguous", "scores", "signals", "contributions")},
        "note": "Deterministic rules first; one Flash call only when the rule margin is thin. "
                "Not wired into the production loop yet.",
    })

    # ── Stage 1: gist ─────────────────────────────────────────────
    gist_prompt = import_gist_prompt(
        imp_id=meta.get("session_id") or "lab",
        project=project,
        cwd=meta.get("cwd") or "",
        branch=meta.get("git_branch") or "",
        started=meta.get("started_at") or "?",
        ended=meta.get("ended_at") or "?",
        dur=int(meta.get("duration_minutes") or 0),
        n_total=int(meta.get("message_count") or 0),
        u=int(meta.get("user_message_count") or 0),
        a=int(meta.get("assistant_message_count") or 0),
        n_used=int(tstats.get("messages_used") or 0),
        n_omit=int(tstats.get("messages_omitted") or 0),
        strategy=tstats.get("strategy") or "all",
        transcript=transcript,
    )
    g = router.complete(gist_prompt, **GIST_PARAMS)
    gist_text = (g.get("text") or "").strip()
    stages.append({
        "name": "gist", "label": "Stage 1: gist (THE CHANGE)",
        "prompt": gist_prompt, "model": g.get("model"), "backend": g.get("backend"),
        "cost_usd": g.get("cost_usd"), "raw_response": g.get("text"),
        "parsed": {"gist": gist_text, "confidence": "med"},
    })

    pi_ctx = fetch_pi_context() if body.get("use_pi_context") else {
        "reachable": False, "themes": [], "patterns": [], "drift": [], "questions": [],
    }

    # ── Stage 2: evidence routing against open questions ─────────
    if body.get("run_routing") and pi_ctx["questions"]:
        r_prompt = build_routing_prompt(gist_text=gist_text, questions=pi_ctx["questions"])
        r = router.complete(r_prompt, **ROUTING_PARAMS)
        decisions = parse_routing_response(r.get("text") or "", len(pi_ctx["questions"]))
        for d in decisions:
            q = pi_ctx["questions"][d["question_index"] - 1]
            d["question"] = q.get("question")
        stages.append({
            "name": "routing", "label": "Stage 2: evidence routing -> open questions",
            "prompt": r_prompt, "model": r.get("model"), "backend": r.get("backend"),
            "cost_usd": r.get("cost_usd"), "raw_response": r.get("text"),
            "parsed": {"decisions": decisions, "n_questions": len(pi_ctx["questions"])},
        })

    # ── Stage 3: insight scan over the gist arc ──────────────────
    if body.get("run_insight", True):
        prior = []
        for i, line in enumerate((body.get("prior_gists") or "").splitlines()):
            line = line.strip()
            if line:
                prior.append({"id": i + 1, "body": line, "confidence": "med",
                              "created_at": ""})
        arc = prior + [{"id": len(prior) + 1, "body": gist_text,
                        "confidence": "med", "created_at": ""}]
        i_prompt = SCAN_PROMPT_TEMPLATE.format(
            project=project,
            window_start=meta.get("started_at") or "?",
            window_end=meta.get("ended_at") or "?",
            gist_count=len(arc),
            existing_themes=_format_existing(pi_ctx["themes"]),
            existing_patterns=_format_existing(pi_ctx["patterns"], key="name"),
            existing_drift=_format_existing(pi_ctx["drift"], key="body"),
            gist_block=_format_gist_block(arc),
        )
        s = router.complete(i_prompt, **INSIGHT_PARAMS)
        insights = parse_scan_response(s.get("text") or "")
        stages.append({
            "name": "insight", "label": "Stage 3: insight scan (themes / patterns / drift)",
            "prompt": i_prompt, "model": s.get("model"), "backend": s.get("backend"),
            "cost_usd": s.get("cost_usd"), "raw_response": s.get("text"),
            "parsed": {"insights": insights, "arc_size": len(arc),
                       "existing_counts": {k: len(pi_ctx[k]) for k in
                                           ("themes", "patterns", "drift")}},
        })

    result = {
        "id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": source_label,
        "project": project,
        "transcript": transcript,
        "pi_context_used": pi_ctx.get("reachable", False),
        "total_cost_usd": round(sum(s.get("cost_usd") or 0 for s in stages), 6),
        "elapsed_s": round(time.time() - t0, 1),
        "stages": stages,
    }
    RUNS_DIR.mkdir(exist_ok=True)
    (RUNS_DIR / f"{result['id']}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def context_status() -> dict:
    secrets, path = _load_secrets()
    manifest = load_manifest_llm()
    dataset = None
    ds_file = HERE / "dataset.json"
    if ds_file.is_file():
        try:
            dataset = json.loads(ds_file.read_text(encoding="utf-8"))
        except Exception:
            dataset = None
    return {
        "openrouter_key": bool(_get_openrouter_api_key(secrets)),
        "secrets_path": str(path) if path else None,
        "models": manifest.get("model_overrides", {}),
        "default_model": manifest.get("model"),
        "pi": fetch_pi_context(),
        "dataset": dataset,
        "runs": list_runs(),
    }


def list_runs() -> list[dict]:
    """Saved runs with enough label info to tell them apart in a dropdown."""
    out = []
    if not RUNS_DIR.is_dir():
        return out
    for p in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": r.get("id", p.stem), "source": r.get("source", "?"),
                        "project": r.get("project", ""),
                        "cost": r.get("total_cost_usd", 0)})
        except Exception:
            out.append({"id": p.stem, "source": "?", "project": "", "cost": 0})
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/context":
            self._json(context_status())
        elif self.path.startswith("/api/runs/"):
            run_id = self.path.rsplit("/", 1)[1]
            f = RUNS_DIR / f"{run_id}.json"
            if f.is_file():
                self._json(json.loads(f.read_text(encoding="utf-8")))
            else:
                self._json({"error": "no such run"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/run":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
            self._json(run_pipeline(body))
        except Exception as e:
            self._json({"error": str(e)}, 500)


def cli_run_one(path: str, use_pi: bool) -> dict:
    r = run_pipeline({
        "mode": "jsonl", "jsonl_path": path, "project": Path(path).parent.name,
        "use_pi_context": use_pi, "run_insight": True, "run_routing": True,
    })
    print(f"\n=== {Path(path).name}  (run {r['id']}, ${r['total_cost_usd']})")
    for st in r["stages"]:
        p = st["parsed"]
        if st["name"] == "transcript":
            s = p["stats"]
            print(f"  transcript: {s['messages_used']}/{s['messages_total']} messages used, "
                  f"{s['messages_omitted']} omitted ({s['strategy']})")
        elif st["name"] == "classify":
            print(f"  CLASS: {p['category']} ({p['confidence']:.2f} conf, "
                  f"{p['method']}, weight {p['weight']}, -> {p['treatment']})")
        elif st["name"] == "gist":
            print(f"  GIST: {p['gist']}")
        elif st["name"] == "routing":
            if p["decisions"]:
                for d in p["decisions"]:
                    print(f"  ROUTE: {d['contribution']} -> {d['question'][:70]}")
            else:
                print("  ROUTE: unfiled")
        elif st["name"] == "insight":
            if p["insights"]:
                for i in p["insights"]:
                    print(f"  {i['kind'].upper()} [{i['confidence']}]: {i['title']}")
            else:
                print("  INSIGHTS: none proposed (bar held)")
    return r


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Pipeline Lab: serve the UI, or run sessions headless.")
    ap.add_argument("--run", metavar="JSONL", help="run one session file and print the result")
    ap.add_argument("--batch", choices=["curated", "random", "imported", "all"],
                    help="run dataset.json sessions (build with build_dataset.py)")
    ap.add_argument("--no-pi", action="store_true", help="skip live Pi context")
    args = ap.parse_args()

    status = context_status()
    if not status["openrouter_key"]:
        print("WARNING: no OpenRouter key (set OPENROUTER_API_KEY or ~/.cortex/secrets.toml)")

    if args.run or args.batch:
        targets = []
        if args.run:
            targets = [args.run]
        else:
            ds = status.get("dataset")
            if not ds:
                print("No dataset.json; run build_dataset.py first.")
                return
            if args.batch in ("curated", "all"):
                targets += [r["path"] for r in ds["curated"]]
            if args.batch in ("random", "all"):
                targets += [r["path"] for r in ds["random"]]
            if args.batch in ("imported", "all"):
                targets += [r["path"] for r in ds.get("imported", [])]
        total = 0.0
        for i, t in enumerate(targets, 1):
            print(f"\n[{i}/{len(targets)}]", end="")
            try:
                total += cli_run_one(t, use_pi=not args.no_pi)["total_cost_usd"]
            except Exception as e:
                print(f"  ERROR: {e}")
        print(f"\nDone: {len(targets)} sessions, ${round(total, 4)} total. "
              f"Runs saved for the UI's load/compare dropdowns.")
        return

    print(f"Pipeline Lab on http://localhost:{PORT}")
    print(f"  OpenRouter key: {'found' if status['openrouter_key'] else 'MISSING'}")
    print(f"  Pi context: {'reachable' if status['pi'].get('reachable') else 'unreachable (existing lists + questions will be empty)'}")
    print(f"  Gist model: {status['models'].get('summarize-session', status['default_model'])}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
