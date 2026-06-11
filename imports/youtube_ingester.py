"""YouTube persona channel ingester (social output channel, 2026-06-11).

Polls a channel's public RSS feed and writes one imported_sessions row
per video, so the existing gist pipeline summarizes Tory's published
videos the same way it summarizes chat sessions and git activity. This
is the first piece of the persona-tracking layer (TURFPTAx /
DuelingGroks / OpenMuscle): what the personas SAY in public, alongside
what Tory ships (git channel) and what he thinks (chat channels).

Design notes:

  - Source tag format: ``youtube:<persona>`` (e.g. ``youtube:turfptax``,
    ``youtube:duelinggroks``). Matches the existing source taxonomy
    (``git:<owner>/<name>``, ``grok-twitter``). One source per persona
    account, so the freshness block's gist origin distribution shows
    each persona as its own channel.

  - Granularity: one row per VIDEO (not per day). Videos are discrete
    published artifacts; a day-bucket would blur titles together.

  - Transport: the channel RSS feed
    (https://www.youtube.com/feeds/videos.xml?channel_id=UC...).
    No API key, no auth, no quota. Returns the ~15 most recent videos.
    Fits the harness rule: external tool, read-only, zero cost.

  - Idempotency: ``file_hash`` is sha256 of the rendered jsonl, which
    deliberately EXCLUDES view counts and the feed's <updated> stamp
    (those churn every poll). Same video, same title + description =
    duplicate = no-op. An edited title/description updates the row in
    place via the PK (INSERT OR REPLACE), same as the git ingester.

  - Schema reuse: writes to overseer.db imported_sessions directly,
    message-shaped jsonl so parse_claude_code_jsonl reads it. The
    ``project`` column carries the persona's project tag (e.g.
    ``dueling-groks``) so gist.created events route to missions.

Explicitly deferred (CP2 candidates):
  - Transcript fetch (adds real content depth; needs youtube-transcript
    -api or timedtext scraping, both fragile from cloud IPs).
  - Deep backfill past the RSS window (needs yt-dlp or Data API).
  - Twitter/X archive importer generalization (separate slice).

CLI (runs ON the Pi; also loadable as a module by the loop wrapper):
    sudo python3 /home/turfptax/cortex-core/imports/youtube_ingester.py \\
        --channel turfptax:UCqUkg2M11LXsoSusLoh20YA:turfptax
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


# Pi-side defaults (same layout as github_ingester.py)
OVERSEER_DB = Path(
    "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db")
IMPORTS_ROOT = Path(
    "/home/turfptax/cortex-core/plugins/overseer/data/imports/youtube")
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def parse_channel_spec(spec: str) -> tuple[str, str, str]:
    """Parse ``persona:channel_id[:project_tag]`` into its parts.

    Channel IDs are [A-Za-z0-9_-] so colon is a safe separator.
    Project defaults to the persona slug when omitted.
    """
    parts = (spec or "").strip().split(":")
    if len(parts) < 2 or not parts[0] or not parts[1].startswith("UC"):
        raise ValueError(
            f"bad channel spec {spec!r} "
            "(want persona:UCxxxx[:project_tag])")
    persona = parts[0].strip().lower()
    channel_id = parts[1].strip()
    project = parts[2].strip() if len(parts) > 2 and parts[2] else persona
    return persona, channel_id, project


def fetch_feed(channel_id: str, timeout: int = 30) -> list[dict]:
    """Fetch + parse the channel RSS feed. Returns a list of video
    dicts (newest first, as the feed orders them)."""
    req = urllib.request.Request(
        FEED_URL.format(channel_id),
        headers={"User-Agent": "cortex-youtube-ingester/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        root = ET.fromstring(resp.read())

    channel_title = (root.findtext("atom:title", "", NS) or "").strip()
    videos = []
    for entry in root.findall("atom:entry", NS):
        video_id = (entry.findtext("yt:videoId", "", NS) or "").strip()
        if not video_id:
            continue
        group = entry.find("media:group", NS)
        description = ""
        if group is not None:
            description = (group.findtext(
                "media:description", "", NS) or "").strip()
        link_el = entry.find("atom:link", NS)
        url = link_el.get("href") if link_el is not None else (
            f"https://www.youtube.com/watch?v={video_id}")
        videos.append({
            "video_id": video_id,
            "title": (entry.findtext("atom:title", "", NS) or "").strip(),
            "published": (entry.findtext(
                "atom:published", "", NS) or "").strip(),
            "url": url,
            "description": description,
            "channel_title": channel_title,
        })
    return videos


def render_video_jsonl(video: dict, persona: str) -> bytes:
    """Render one video as a single message-shaped jsonl line.

    Only stable fields go in (no view counts, no feed <updated>), so
    the sha256 of this content is a stable idempotency key. The
    ``_youtube_video`` sidecar keeps the structured data for future
    re-parsers, same pattern as ``_git_event`` in the git ingester.
    """
    desc = video.get("description") or ""
    if len(desc) > 4000:
        desc = desc[:4000] + " [...]"
    text = (
        f"[youtube video published | persona {persona} | "
        f"channel {video.get('channel_title', '?')}]\n"
        f"Title: {video.get('title', '')}\n"
        f"URL: {video.get('url', '')}\n"
        f"Published: {video.get('published', '')}\n"
        f"Description:\n{desc}"
    )
    wrapped = {
        "type": "user",
        "timestamp": video.get("published"),
        "message": {"role": "user", "content": text},
        "_youtube_video": {
            "video_id": video.get("video_id"),
            "title": video.get("title"),
            "published": video.get("published"),
            "url": video.get("url"),
            "description": desc,
            "channel_title": video.get("channel_title"),
            "persona": persona,
        },
    }
    line = json.dumps(wrapped, sort_keys=True, ensure_ascii=False)
    return (line + "\n").encode("utf-8")


def insert_imported_session(
    db_path: Path,
    *,
    row_id: str,
    source: str,
    source_path: str,
    project: str,
    started_at: str,
    bytes_size: int,
    file_hash: str,
    metadata: dict,
) -> str:
    """INSERT OR IGNORE into imported_sessions. Returns
    'inserted' | 'duplicate' | 'updated'. Same dedup contract as the
    git ingester: (source, file_hash) UNIQUE catches same-content
    re-runs; PK REPLACE catches edited title/description."""
    metadata_json = json.dumps(metadata, sort_keys=True, ensure_ascii=False)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        existing = cur.execute(
            "SELECT id FROM imported_sessions "
            "WHERE source = ? AND file_hash = ? LIMIT 1",
            (source, file_hash),
        ).fetchone()
        if existing:
            return "duplicate"
        pk_existed = cur.execute(
            "SELECT id FROM imported_sessions WHERE id = ? LIMIT 1",
            (row_id,),
        ).fetchone() is not None
        cur.execute("""
            INSERT OR REPLACE INTO imported_sessions (
                id, source, source_path, project, cwd, git_branch,
                started_at, ended_at, duration_minutes,
                message_count, user_message_count, assistant_message_count,
                tool_use_count, bytes_size, file_hash, metadata_json,
                imported_at
            ) VALUES (?, ?, ?, ?, '', '', ?, ?, 0, 1, 0, 0, 0, ?, ?, ?,
                      datetime('now'))
        """, (
            row_id, source, source_path, project,
            started_at, started_at, bytes_size, file_hash, metadata_json,
        ))
        conn.commit()
        return "updated" if pk_existed else "inserted"
    finally:
        conn.close()


def ingest_channel(channel_id: str, persona: str, project: str,
                   db_path: Path = OVERSEER_DB,
                   imports_root: Path = IMPORTS_ROOT) -> dict:
    """End-to-end ingest for one channel's current RSS window.

    Returns a summary including ``new_videos`` (inserted this run only)
    so the loop wrapper can publish social.post.created events for
    genuinely new uploads and stay silent on backfill duplicates.
    """
    print(f"=== ingesting youtube:{persona} ({channel_id}) ===")
    videos = fetch_feed(channel_id)
    print(f"  feed videos: {len(videos)}")

    source = f"youtube:{persona}"
    channel_dir = imports_root / persona

    inserted = 0
    duplicates = 0
    updated = 0
    new_videos = []
    for video in videos:
        vid = video["video_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
            print(f"    [?] skipping odd video id {vid!r}")
            continue
        content = render_video_jsonl(video, persona)
        jsonl_path = channel_dir / f"{vid}.jsonl"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_path.write_bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()

        result = insert_imported_session(
            db_path,
            row_id=f"youtube:{persona}:{vid}",
            source=source,
            source_path=str(jsonl_path),
            project=project,
            started_at=video.get("published")
            or "1970-01-01T00:00:00+00:00",
            bytes_size=len(content),
            file_hash=file_hash,
            metadata={
                "kind": "youtube",
                "persona": persona,
                "channel_id": channel_id,
                "channel_title": video.get("channel_title"),
                "video_id": vid,
                "title": video.get("title"),
                "published": video.get("published"),
                "url": video.get("url"),
            },
        )
        marker = {"inserted": "+", "duplicate": ".",
                  "updated": "~"}.get(result, "?")
        safe_title = (video.get("title") or "")[:70].encode(
            "ascii", "replace").decode()
        print(f"    [{marker}] {vid} {safe_title}")
        if result == "inserted":
            inserted += 1
            new_videos.append({"id": vid, "title": video.get("title")})
        elif result == "duplicate":
            duplicates += 1
        else:
            updated += 1

    summary = {
        "persona": persona,
        "channel_id": channel_id,
        "project": project,
        "source": source,
        "videos_seen": len(videos),
        "rows_inserted": inserted,
        "rows_duplicate": duplicates,
        "rows_updated": updated,
        "new_videos": new_videos,
    }
    print(json.dumps({k: v for k, v in summary.items()
                      if k != "new_videos"}, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Ingest a YouTube channel's RSS window into "
                    "overseer.db imported_sessions.")
    parser.add_argument(
        "--channel", required=True,
        help="persona:channel_id[:project_tag] "
             "(e.g. turfptax:UCqUkg2M11LXsoSusLoh20YA:turfptax)")
    parser.add_argument("--db", type=Path, default=OVERSEER_DB)
    parser.add_argument("--imports-root", type=Path, default=IMPORTS_ROOT)
    args = parser.parse_args()

    try:
        persona, channel_id, project = parse_channel_spec(args.channel)
    except ValueError as e:
        sys.exit(str(e))
    ingest_channel(channel_id, persona, project,
                   db_path=args.db, imports_root=args.imports_root)


if __name__ == "__main__":
    main()
