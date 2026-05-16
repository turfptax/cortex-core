"""Slice 9.4 CP1 — GitHub repo ingester (action-channel for overseer).

Pulls a repo's recent git activity (commits, tags, issues, PRs, workflow
runs) into the overseer's `imported_sessions` table so the existing gist
pipeline summarizes it the same way it summarizes chat sessions. Each
day with activity becomes one imported_session row + one .jsonl file.

Design notes (locked 2026-05-16 — see memory/agent_ecosystem_design.md
and the overseer chat thread for the reasoning):

  - Source tag format: ``git:<owner>/<name>`` (e.g.
    ``git:Open-Muscle/OpenMuscle-Software``). NEW origin in the source
    taxonomy, deliberately distinct from the existing ``chatgpt``,
    ``claude-code``, ``grok-com``, ``grok-twitter``, ``twitter``,
    ``ble``, ``voice`` sources. Overseer wants the asymmetry: git
    evidence vs chat evidence as separate channels in the freshness
    block's gist origin distribution.

  - Granularity: one row per (repo, day with activity). Matches the
    one-conversation-per-row chat pattern.

  - Schema reuse: writes to ``overseer.db.imported_sessions`` directly.
    No new tables. The gist pipeline's ``_summarize_imported_sessions``
    reads from there and produces a summary gist via LLM. We just make
    sure the .jsonl content is readable as a chronological event stream.

  - Idempotency: ``file_hash`` is sha256 of the rendered jsonl. The
    existing ``idx_imported_hash`` UNIQUE(source, file_hash) means
    re-running with no new events is a no-op.

  - Runs ON .25. PAT lives at /home/turfptax/.cortex/secrets.toml in a
    [github] section. cortex.db / overseer.db also on .25. No external
    deps — stdlib only.

CP1 explicitly defers:
  - Multi-repo loop (run with --repo <owner/name> per call)
  - Auto-discovery / search
  - Periodic scheduling (cron, loop-tick) — manual run for now
  - Per-commit files-changed detail (extra API call per commit; not
    worth it for CP1 — message + author + ts is the load-bearing part)
  - Hardware-adjacent artifact detection (KiCad, gerbers, BOMs).
    Tracked as a CP2 follow-up.

CLI:
    sudo python3 /home/turfptax/cortex-core/imports/github_ingester.py \\
        --repo Open-Muscle/OpenMuscle-Software \\
        --days 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


# ── Paths (Pi-side defaults) ────────────────────────────────────────────

SECRETS_PATH = Path("/home/turfptax/.cortex/secrets.toml")
OVERSEER_DB = Path("/home/turfptax/cortex-core/plugins/overseer/data/overseer.db")
IMPORTS_ROOT = Path(
    "/home/turfptax/cortex-core/plugins/overseer/data/imports/git"
)
GITHUB_API = "https://api.github.com"


# ── PAT loader ──────────────────────────────────────────────────────────


def load_pat(path: Path = SECRETS_PATH) -> str:
    """Extract the GitHub PAT from secrets.toml's [github] section.

    Uses a small regex rather than tomllib so this stays runnable on
    pre-3.11 interpreters if needed.
    """
    text = path.read_text(encoding="utf-8")
    m = re.search(r'\[github\][^\[]*?pat\s*=\s*"([^"]+)"', text, re.DOTALL)
    if not m:
        sys.exit(f"[github].pat not found in {path}")
    return m.group(1)


# ── GitHub API helpers ──────────────────────────────────────────────────


def gh_request(pat: str, path: str, **params) -> Any:
    """GET a GitHub API path with auth. Returns parsed JSON (list or dict).

    Raises on non-2xx so callers can fail loud. params are URL-encoded.
    """
    qs = urllib.parse.urlencode({k: v for k, v in params.items()
                                  if v is not None})
    url = f"{GITHUB_API}{path}"
    if qs:
        url = f"{url}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cortex-github-ingester/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300] if hasattr(e, "read") else b""
        sys.exit(
            f"GitHub API {e.code} on {path} (params={params!r}): "
            f"{body.decode('utf-8', errors='replace')}"
        )


def gh_paginate(pat: str, path: str, **params) -> Iterable[dict]:
    """Yield items from a paginated GitHub list endpoint."""
    page = 1
    while True:
        data = gh_request(pat, path, page=page, per_page=100, **params)
        if not isinstance(data, list):
            sys.exit(f"expected list from {path}, got {type(data).__name__}")
        if not data:
            return
        yield from data
        if len(data) < 100:
            return
        page += 1
        if page > 50:  # 5000 items cap — protects against runaway pagination
            return


# ── Event fetchers ──────────────────────────────────────────────────────


def fetch_commits(pat: str, owner: str, repo: str,
                  since_iso: str, until_iso: str) -> list[dict]:
    """List commits in the date range, default-branch only.

    The /commits endpoint returns commits in DESC chronological order
    by author date. We pass since/until to bound the window.
    """
    events = []
    for c in gh_paginate(
        pat, f"/repos/{owner}/{repo}/commits",
        since=since_iso, until=until_iso,
    ):
        # The shape: {sha, commit: {author: {name, date}, message, ...},
        #              author: {login}, html_url, ...}
        commit_obj = c.get("commit") or {}
        author_obj = commit_obj.get("author") or {}
        ts = author_obj.get("date")  # ISO 8601 UTC
        msg = (commit_obj.get("message") or "").strip()
        first_line = msg.split("\n", 1)[0][:200]
        events.append({
            "ts": ts,
            "type": "commit",
            "sha": c.get("sha", "")[:12],
            "author": author_obj.get("name", ""),
            "login": (c.get("author") or {}).get("login", ""),
            "summary": f"commit {c.get('sha', '')[:8]} — {first_line}",
            "data": {
                "sha": c.get("sha"),
                "message": msg,
                "url": c.get("html_url"),
                "parents": [p.get("sha", "")[:12]
                            for p in (c.get("parents") or [])],
            },
        })
    return events


def fetch_tags(pat: str, owner: str, repo: str,
               since_iso: str) -> list[dict]:
    """Tags don't carry timestamps directly. We fetch recent tags + the
    commit SHA each points at, then look up that commit's date and
    filter to since_iso. Cheap because tag list is small."""
    out = []
    try:
        tags = gh_request(pat, f"/repos/{owner}/{repo}/tags", per_page=30)
    except SystemExit:
        return out
    if not isinstance(tags, list):
        return out
    for t in tags[:30]:
        sha = (t.get("commit") or {}).get("sha")
        if not sha:
            continue
        # one commit lookup per tag — bounded at 30 calls total
        try:
            c = gh_request(pat, f"/repos/{owner}/{repo}/commits/{sha}")
        except SystemExit:
            continue
        ts = ((c.get("commit") or {}).get("author") or {}).get("date")
        if not ts or ts < since_iso:
            continue
        out.append({
            "ts": ts,
            "type": "tag",
            "name": t.get("name"),
            "sha": sha[:12],
            "summary": f"tag {t.get('name')} → {sha[:8]}",
            "data": {
                "name": t.get("name"),
                "sha": sha,
                "zipball_url": t.get("zipball_url"),
            },
        })
    return out


def fetch_issues_and_prs(pat: str, owner: str, repo: str,
                         since_iso: str) -> list[dict]:
    """The /issues endpoint returns BOTH issues and PRs (GitHub treats
    PRs as issues). We split them via the `pull_request` field presence.
    `since` filters by *updated* date, which captures new + reopened +
    commented since the window — good for activity tracking."""
    out = []
    for it in gh_paginate(
        pat, f"/repos/{owner}/{repo}/issues",
        state="all", since=since_iso,
    ):
        is_pr = "pull_request" in it and it["pull_request"] is not None
        ev_type = "pr" if is_pr else "issue"
        state = it.get("state", "?")
        # PRs additionally carry a merged_at; flag merged distinctly
        if is_pr and (it.get("pull_request") or {}).get("merged_at"):
            state = "merged"
        title = (it.get("title") or "").strip()[:200]
        ts = it.get("updated_at") or it.get("created_at")
        n = it.get("number")
        out.append({
            "ts": ts,
            "type": ev_type,
            "number": n,
            "state": state,
            "title": title,
            "summary": f"{ev_type} #{n} ({state}) — {title}",
            "data": {
                "number": n,
                "state": state,
                "title": title,
                "url": it.get("html_url"),
                "user": (it.get("user") or {}).get("login"),
                "created_at": it.get("created_at"),
                "updated_at": it.get("updated_at"),
                "closed_at": it.get("closed_at"),
                "comments": it.get("comments", 0),
                "labels": [l.get("name") for l in (it.get("labels") or [])],
            },
        })
    return out


def fetch_workflow_runs(pat: str, owner: str, repo: str,
                        since_iso: str) -> list[dict]:
    """CI runs in the window. Conclusion tells us pass/fail history."""
    out = []
    try:
        data = gh_request(
            pat, f"/repos/{owner}/{repo}/actions/runs",
            created=f">={since_iso[:10]}", per_page=100,
        )
    except SystemExit:
        return out
    if not isinstance(data, dict):
        return out
    for r in (data.get("workflow_runs") or [])[:200]:
        ts = r.get("created_at")
        out.append({
            "ts": ts,
            "type": "workflow_run",
            "name": r.get("name"),
            "conclusion": r.get("conclusion"),
            "status": r.get("status"),
            "summary": (f"workflow '{r.get('name')}' "
                        f"({r.get('conclusion') or r.get('status')})"),
            "data": {
                "id": r.get("id"),
                "name": r.get("name"),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "branch": r.get("head_branch"),
                "head_sha": (r.get("head_sha") or "")[:12],
                "url": r.get("html_url"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            },
        })
    return out


# ── Day bucketing + jsonl write ─────────────────────────────────────────


def bucket_by_day(events: list[dict]) -> dict[str, list[dict]]:
    """Group events by their UTC date (YYYY-MM-DD). Events without a
    parseable timestamp are dropped (logged)."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    dropped = 0
    for ev in events:
        ts = ev.get("ts")
        if not ts:
            dropped += 1
            continue
        # ts is ISO 8601 — slice the date portion
        date = ts[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            dropped += 1
            continue
        buckets[date].append(ev)
    if dropped:
        print(f"  dropped {dropped} events without parseable timestamps")
    # Sort each day's events chronologically
    for date in buckets:
        buckets[date].sort(key=lambda e: e.get("ts", ""))
    return dict(buckets)


def _event_to_message_text(ev: dict) -> str:
    """Render one git event as plain text for the LLM gist prompt.

    The gist pipeline reads each .jsonl line as a chat message; we shape
    git events into that container while keeping the human-readable
    detail intact. Commit messages get the full body (truncated for
    safety); tags/issues/PRs/workflow runs get a structured one-line +
    relevant fields.
    """
    t = ev.get("type")
    summary = ev.get("summary", "")
    if t == "commit":
        data = ev.get("data") or {}
        author = ev.get("author") or ev.get("login") or "?"
        ts = ev.get("ts") or "?"
        sha = ev.get("sha") or "?"
        full_msg = (data.get("message") or "").strip()
        # Cap individual commit body at 4000 chars so one giant commit
        # doesn't dominate the day's transcript window.
        if len(full_msg) > 4000:
            full_msg = full_msg[:4000] + " […]"
        return (
            f"[commit {sha} | {author} | {ts}]\n{full_msg}"
        )
    if t == "tag":
        return (
            f"[tag {ev.get('name')} @ {ev.get('sha')} | {ev.get('ts')}] "
            f"{summary}"
        )
    if t in ("issue", "pr"):
        data = ev.get("data") or {}
        labels = data.get("labels") or []
        labels_str = (", labels=" + "/".join(labels)) if labels else ""
        return (
            f"[{t} #{ev.get('number')} {ev.get('state')} | "
            f"updated {ev.get('ts')}]"
            f" {ev.get('title', '')}{labels_str}\n"
            f"  url: {data.get('url')}"
            f"  comments: {data.get('comments', 0)}"
        )
    if t == "workflow_run":
        data = ev.get("data") or {}
        return (
            f"[workflow_run {ev.get('name')} ({ev.get('conclusion') or ev.get('status')}) "
            f"| branch={data.get('branch')} sha={data.get('head_sha')} "
            f"| {ev.get('ts')}]"
        )
    # Unknown event type — fallback
    return f"[{t}] {summary}"


def write_day_jsonl(dest: Path, events: list[dict]) -> tuple[str, int]:
    """Write events to a .jsonl file, message-shaped so the existing
    overseer gist pipeline (parse_claude_code_jsonl + import_gist_prompt)
    reads them as a chronological transcript.

    Each line has the structure the chat-side parser expects:
        {
          "type": "user",
          "timestamp": "<iso>",
          "message": {"role": "user", "content": "<rendered text>"}
          "_git_event": {<original event dict for future readers>}
        }

    `_git_event` is ignored by parse_claude_code_jsonl (it only reads
    type/timestamp/message) but preserved so anyone re-parsing this file
    later (a CP3 git-specific summarizer, debugging, etc.) has the
    structured data intact. Returns (sha256_hex, size_bytes).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for ev in events:
        text = _event_to_message_text(ev)
        wrapped = {
            "type": "user",
            "timestamp": ev.get("ts"),
            "message": {"role": "user", "content": text},
            "_git_event": ev,
        }
        lines.append(json.dumps(wrapped, sort_keys=True, ensure_ascii=False))
    content = ("\n".join(lines) + "\n").encode("utf-8")
    dest.write_bytes(content)
    return hashlib.sha256(content).hexdigest(), len(content)


# ── DB write ────────────────────────────────────────────────────────────


def insert_imported_session(
    db_path: Path,
    *,
    row_id: str,
    source: str,
    source_path: str,
    project: str,
    git_branch: str,
    started_at: str,
    ended_at: str,
    duration_minutes: int,
    message_count: int,
    bytes_size: int,
    file_hash: str,
    metadata: dict,
) -> str:
    """INSERT OR IGNORE into imported_sessions. Returns one of:
    'inserted' | 'duplicate' | 'updated'.

    Duplicate detection is via the (source, file_hash) UNIQUE index —
    same content on re-run is a no-op. The row_id is also unique by
    PRIMARY KEY, which catches same-day-different-content (e.g. a new
    commit landed since last run): we INSERT OR REPLACE on PK collision
    so the row updates in place.
    """
    metadata_json = json.dumps(metadata, sort_keys=True, ensure_ascii=False)
    conn = sqlite3.connect(str(db_path))
    try:
        # First check if this exact (source, file_hash) already exists:
        # the UNIQUE index will refuse inserts but we want to know which.
        cur = conn.cursor()
        existing = cur.execute(
            "SELECT id FROM imported_sessions "
            "WHERE source = ? AND file_hash = ? LIMIT 1",
            (source, file_hash),
        ).fetchone()
        if existing:
            return "duplicate"

        # Otherwise INSERT OR REPLACE on PK — handles same-day-with-
        # new-commits cleanly.
        cur.execute("""
            INSERT OR REPLACE INTO imported_sessions (
                id, source, source_path, project, cwd, git_branch,
                started_at, ended_at, duration_minutes,
                message_count, user_message_count, assistant_message_count,
                tool_use_count, bytes_size, file_hash, metadata_json,
                imported_at
            ) VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?,
                      datetime('now'))
        """, (
            row_id, source, source_path, project, git_branch,
            started_at, ended_at, duration_minutes,
            message_count, bytes_size, file_hash, metadata_json,
        ))
        conn.commit()
        return "inserted"
    finally:
        conn.close()


# ── Orchestration ──────────────────────────────────────────────────────


def repo_default_branch(pat: str, owner: str, repo: str) -> str:
    info = gh_request(pat, f"/repos/{owner}/{repo}")
    return (info or {}).get("default_branch", "main")


def ingest_repo(pat: str, owner: str, repo: str, days: int,
                db_path: Path = OVERSEER_DB,
                imports_root: Path = IMPORTS_ROOT) -> dict:
    """End-to-end ingest for one repo over the last N days."""
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"=== ingesting {owner}/{repo} (last {days} days) ===")
    print(f"  since: {since_iso}")
    print(f"  until: {until_iso}")

    default_branch = repo_default_branch(pat, owner, repo)
    print(f"  default branch: {default_branch}")

    print("  fetching commits…")
    commits = fetch_commits(pat, owner, repo, since_iso, until_iso)
    print(f"    {len(commits)} commits")

    print("  fetching tags…")
    tags = fetch_tags(pat, owner, repo, since_iso)
    print(f"    {len(tags)} tags in window")

    print("  fetching issues + PRs…")
    issues_prs = fetch_issues_and_prs(pat, owner, repo, since_iso)
    n_issues = sum(1 for e in issues_prs if e["type"] == "issue")
    n_prs = sum(1 for e in issues_prs if e["type"] == "pr")
    print(f"    {n_issues} issues, {n_prs} PRs")

    print("  fetching workflow runs…")
    workflow_runs = fetch_workflow_runs(pat, owner, repo, since_iso)
    print(f"    {len(workflow_runs)} workflow runs")

    all_events = commits + tags + issues_prs + workflow_runs
    print(f"  total events: {len(all_events)}")

    buckets = bucket_by_day(all_events)
    print(f"  days with activity: {len(buckets)}")

    source = f"git:{owner}/{repo}"
    repo_imports_dir = imports_root / owner / repo

    inserted = 0
    duplicates = 0
    updated = 0
    for date in sorted(buckets.keys()):
        events = buckets[date]
        if not events:
            continue
        # Per-day file
        jsonl_path = repo_imports_dir / f"{date}.jsonl"
        file_hash, size_bytes = write_day_jsonl(jsonl_path, events)

        # Build the imported_session row
        row_id = f"git:{owner}/{repo}:{date}"
        timestamps = [e.get("ts") for e in events if e.get("ts")]
        started_at = min(timestamps) if timestamps else f"{date}T00:00:00Z"
        ended_at = max(timestamps) if timestamps else f"{date}T23:59:59Z"

        # Day-bucketed activity has no meaningful "duration" — set 0,
        # callers can compute span from started/ended if needed.
        n_commits = sum(1 for e in events if e["type"] == "commit")
        n_tags = sum(1 for e in events if e["type"] == "tag")
        n_iss = sum(1 for e in events if e["type"] == "issue")
        n_prs_day = sum(1 for e in events if e["type"] == "pr")
        n_wf = sum(1 for e in events if e["type"] == "workflow_run")

        metadata = {
            "kind": "git",
            "owner": owner,
            "repo": repo,
            "date": date,
            "default_branch": default_branch,
            "counts": {
                "commits": n_commits,
                "tags": n_tags,
                "issues": n_iss,
                "prs": n_prs_day,
                "workflow_runs": n_wf,
            },
            "latest_sha": next(
                (e["sha"] for e in reversed(events)
                 if e["type"] == "commit"), None,
            ),
        }
        result = insert_imported_session(
            db_path,
            row_id=row_id,
            source=source,
            source_path=str(jsonl_path),
            project=repo,
            git_branch=default_branch,
            started_at=started_at,
            ended_at=ended_at,
            duration_minutes=0,
            message_count=len(events),
            bytes_size=size_bytes,
            file_hash=file_hash,
            metadata=metadata,
        )
        marker = {"inserted": "+", "duplicate": ".",
                  "updated": "~"}.get(result, "?")
        print(f"    [{marker}] {date} — {len(events):3d} events "
              f"({n_commits}c {n_tags}t {n_iss}i {n_prs_day}p {n_wf}w)")
        if result == "inserted":
            inserted += 1
        elif result == "duplicate":
            duplicates += 1
        else:
            updated += 1

    summary = {
        "repo": f"{owner}/{repo}",
        "source": source,
        "days_with_activity": len(buckets),
        "rows_inserted": inserted,
        "rows_duplicate": duplicates,
        "rows_updated": updated,
        "total_events": len(all_events),
        "commits": len(commits),
        "tags": len(tags),
        "issues": n_issues,
        "prs": n_prs,
        "workflow_runs": len(workflow_runs),
    }
    print()
    print("=== summary ===")
    print(json.dumps(summary, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Ingest a GitHub repo's recent activity into "
                    "overseer.db imported_sessions.",
    )
    parser.add_argument(
        "--repo", required=True,
        help="owner/name (e.g. Open-Muscle/OpenMuscle-Software)",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Lookback window in days (default 30)",
    )
    parser.add_argument(
        "--secrets", type=Path, default=SECRETS_PATH,
        help=f"Path to secrets.toml (default {SECRETS_PATH})",
    )
    parser.add_argument(
        "--db", type=Path, default=OVERSEER_DB,
        help=f"Path to overseer.db (default {OVERSEER_DB})",
    )
    parser.add_argument(
        "--imports-root", type=Path, default=IMPORTS_ROOT,
        help=f"Where to write .jsonl files (default {IMPORTS_ROOT})",
    )
    args = parser.parse_args()

    if "/" not in args.repo:
        sys.exit("--repo must be in owner/name format")
    owner, repo = args.repo.split("/", 1)

    pat = load_pat(args.secrets)
    ingest_repo(pat, owner, repo, args.days,
                db_path=args.db, imports_root=args.imports_root)


if __name__ == "__main__":
    main()
