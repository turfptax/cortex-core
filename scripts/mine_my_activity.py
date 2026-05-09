"""Slice 9 Phase 7 — Google "My Activity" HTML import.

Parses all per-service MyActivity.html files from a Google Takeout
extraction and lands them across two tables on overseer.db:

  activity_events     — raw row per activity item (query, URL,
                        timestamp, service, payload)
  activity_daily      — per (date, service) rollup with top-N + a
                        novelty_score (fraction of today's
                        queries/domains not seen in prior 30 days).
                        That's where the actual signal lives — top-N
                        is mostly the same domains every day, but
                        novelty is what surfaces new threads.

Services covered (pulled by folder name under My Activity/):
  Search, YouTube, Chrome, Google News, Maps, Image Search, Gemini

Run on Pi:
  sudo python3 mine_my_activity.py /tmp/my-activity

  (Where /tmp/my-activity contains subdirs named for each service,
  each holding a MyActivity.html file.)
"""
from __future__ import annotations

import collections
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

OVERSEER_DB = "/home/turfptax/cortex-core/plugins/overseer/data/overseer.db"
AGENT_TAG = "my-activity-import"

# 30-day rolling window for novelty scoring
NOVELTY_WINDOW_DAYS = 30

# Folder name → service tag mapping
SERVICE_MAP = {
    "Search": "search",
    "YouTube": "youtube",
    "Chrome": "chrome",
    "Google News": "google-news",
    "Maps": "maps",
    "Image Search": "image-search",
    "Gemini Apps": "gemini",
    "Assistant": "assistant",
    "Android": "android",
    "Google Play Store": "play-store",
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    action TEXT,
    timestamp_local TEXT,
    timestamp_utc TEXT NOT NULL,
    date TEXT NOT NULL,
    payload TEXT,
    payload_url TEXT,
    payload_domain TEXT,
    payload_text TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    imported_by_agent TEXT
);
CREATE INDEX IF NOT EXISTS activity_events_service ON activity_events(service);
CREATE INDEX IF NOT EXISTS activity_events_date ON activity_events(date);
CREATE INDEX IF NOT EXISTS activity_events_domain ON activity_events(payload_domain);
CREATE TABLE IF NOT EXISTS activity_daily (
    date TEXT NOT NULL,
    service TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    top_domains_json TEXT,
    top_queries_json TEXT,
    top_channels_json TEXT,
    novelty_score REAL,
    novelty_items_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    imported_by_agent TEXT,
    PRIMARY KEY (date, service)
);
"""


# ── HTML parsing ────────────────────────────────────────────────

# Match outer-cell items
OUTER_CELL_RE = re.compile(
    r'<div class="outer-cell[^"]*"[^>]*>(.*?)</div></div></div>',
    re.DOTALL,
)
# Title (action verb) — inside header-cell <p class="mdl-typography--title">
TITLE_RE = re.compile(
    r'<p class="mdl-typography--title">([^<]*?)<br',
)
# Content cell with body-1 class — has activity content + timestamp
CONTENT_CELL_RE = re.compile(
    r'<div class="content-cell[^"]*body-1[^"]*">(.*?)</div>',
    re.DOTALL,
)
# Timestamp pattern: "May 7, 2026, 9:30:43 AM CDT"
TIMESTAMP_RE = re.compile(
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)\s*[A-Z]+'
)
URL_RE = re.compile(r'href="([^"]+)"')
TAG_RE = re.compile(r'<[^>]+>')

MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

TZ_OFFSETS = {  # rough — Tory is in CDT/CST
    "CDT": -5, "CST": -6, "EDT": -4, "EST": -5,
    "PDT": -7, "PST": -8, "UTC": 0, "GMT": 0,
    "MDT": -6, "MST": -7,
}


def parse_timestamp(ts_str: str) -> tuple[str, str] | None:
    """'May 7, 2026, 9:30:43 AM CDT' → ('2026-05-07T14:30:43Z', '2026-05-07')."""
    m = re.match(
        r'(\w{3})\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2}):(\d{2})\s+(AM|PM)\s+(\w+)',
        ts_str.strip(),
    )
    if not m:
        return None
    mo, dd, yyyy, hh, mm, ss, ampm, tz = m.groups()
    month = MONTH_MAP.get(mo)
    if not month:
        return None
    hour = int(hh) % 12 + (12 if ampm == "PM" else 0)
    offset = TZ_OFFSETS.get(tz, 0)
    try:
        local_dt = datetime(int(yyyy), month, int(dd), hour, int(mm), int(ss))
    except ValueError:
        return None
    utc_dt = local_dt - timedelta(hours=offset)
    return (
        utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        utc_dt.strftime("%Y-%m-%d"),
    )


def strip_tags(s: str) -> str:
    return TAG_RE.sub(' ', s).replace('&nbsp;', ' ').replace('&amp;', '&').replace('&emsp;', ' ').strip()


def parse_one_item(item_html: str, service: str) -> dict | None:
    """Parse a single outer-cell HTML chunk."""
    title_m = TITLE_RE.search(item_html)
    action = title_m.group(1).strip() if title_m else None
    content_m = CONTENT_CELL_RE.search(item_html)
    if not content_m:
        return None
    content = content_m.group(1)
    ts_m = TIMESTAMP_RE.search(content)
    if not ts_m:
        return None
    parsed = parse_timestamp(ts_m.group(0))
    if not parsed:
        return None
    ts_utc, date = parsed
    # Strip the timestamp from content for cleaner payload
    payload_html = content[:ts_m.start()] + content[ts_m.end():]
    payload_text = re.sub(r'\s+', ' ', strip_tags(payload_html)).strip()
    # First URL if any
    url_m = URL_RE.search(payload_html)
    url = url_m.group(1) if url_m else None
    domain = None
    if url:
        try:
            domain = urlparse(url).netloc.lower() or None
        except Exception:
            pass
    return {
        "service": service,
        "action": action,
        "timestamp_local": ts_m.group(0),
        "timestamp_utc": ts_utc,
        "date": date,
        "payload": payload_text[:5000],
        "payload_url": url,
        "payload_domain": domain,
        "payload_text": payload_text[:5000],
    }


def parse_html(path: Path, service: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    out: list[dict] = []
    for m in OUTER_CELL_RE.finditer(html):
        item = parse_one_item(m.group(1), service)
        if item:
            out.append(item)
    return out


# ── Insertion ──────────────────────────────────────────────────

def bulk_insert_events(db: sqlite3.Connection, events: list[dict]) -> int:
    db.executemany(
        """
        INSERT INTO activity_events
          (service, action, timestamp_local, timestamp_utc, date,
           payload, payload_url, payload_domain, payload_text,
           imported_by_agent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                e["service"], e["action"], e["timestamp_local"],
                e["timestamp_utc"], e["date"], e["payload"],
                e["payload_url"], e["payload_domain"],
                e["payload_text"], AGENT_TAG,
            )
            for e in events
        ],
    )
    return len(events)


# ── Daily rollups + novelty ───────────────────────────────────

def compute_daily_rollups(db: sqlite3.Connection) -> int:
    """For each (date, service) recompute event_count + top-N + novelty_score."""
    print("\n--- aggregating per (date, service) ---")
    db.execute("DELETE FROM activity_daily")
    db.commit()

    pairs = list(db.execute(
        "SELECT DISTINCT date, service FROM activity_events ORDER BY date"
    ))
    total = len(pairs)

    rollups_inserted = 0
    for i, (date, service) in enumerate(pairs):
        # Today's items
        rows = list(db.execute(
            """
            SELECT payload_domain, payload_text
            FROM activity_events
            WHERE date=? AND service=?
            """,
            (date, service),
        ))
        n = len(rows)

        domains = collections.Counter(
            r[0] for r in rows if r[0]
        ).most_common(10)
        queries = collections.Counter(
            (r[1] or "")[:200] for r in rows if r[1]
        ).most_common(10)
        # Channels (youtube only — extract from URL/payload)
        channels: list = []
        if service == "youtube":
            chan_counter: collections.Counter = collections.Counter()
            for r in rows:
                p = r[1] or ""
                m = re.search(r'(?:Watched\s+).*?(?:\son\s+|\s–\s)([^–\s][^,]*?)(?:\s|$)', p)
                if m:
                    chan_counter[m.group(1).strip()] += 1
            channels = chan_counter.most_common(10)

        # Novelty: fraction of today's domains+queries not in prior 30d
        cutoff = (
            datetime.fromisoformat(date) - timedelta(days=NOVELTY_WINDOW_DAYS)
        ).strftime("%Y-%m-%d")
        prior_set = set()
        for prow in db.execute(
            """
            SELECT DISTINCT payload_domain, payload_text
            FROM activity_events
            WHERE service=? AND date >= ? AND date < ?
            """,
            (service, cutoff, date),
        ):
            if prow[0]:
                prior_set.add(("dom", prow[0]))
            if prow[1]:
                prior_set.add(("q", (prow[1] or "")[:200]))
        today_set = set()
        for r in rows:
            if r[0]:
                today_set.add(("dom", r[0]))
            if r[1]:
                today_set.add(("q", (r[1] or "")[:200]))
        novel = today_set - prior_set
        novelty_score = (len(novel) / len(today_set)) if today_set else None
        novelty_items = sorted(
            [v for k, v in novel],
        )[:30]

        db.execute(
            """
            INSERT INTO activity_daily
              (date, service, event_count, top_domains_json,
               top_queries_json, top_channels_json,
               novelty_score, novelty_items_json,
               imported_by_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date, service, n,
                json.dumps(domains),
                json.dumps(queries),
                json.dumps(channels),
                novelty_score,
                json.dumps(novelty_items),
                AGENT_TAG,
            ),
        )
        rollups_inserted += 1
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{total}")
            db.commit()
    db.commit()
    return rollups_inserted


def upsert_horizon(db: sqlite3.Connection) -> None:
    res = db.execute(
        "SELECT MIN(date), MAX(date), COUNT(*) FROM activity_events"
    ).fetchone()
    if not res or not res[0]:
        return
    e, l, n = res
    notes = (
        f"My Activity import: {n} events across "
        f"{db.execute('SELECT COUNT(DISTINCT service) FROM activity_events').fetchone()[0]} "
        f"services. Range {e} → {l}. Daily rollups carry the "
        f"novelty_score field — that's the working-memory signal "
        f"layer; raw event table is for query."
    )
    db.execute(
        """
        INSERT INTO data_horizons (surface, earliest_data, latest_data, notes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(surface) DO UPDATE SET
          earliest_data=excluded.earliest_data,
          latest_data=excluded.latest_data,
          notes=excluded.notes,
          updated_at=CURRENT_TIMESTAMP
        """,
        ("activity_events", e, l, notes),
    )


def main(root: str) -> int:
    base = Path(root)
    if not base.is_dir():
        print(f"ERR: {root} not found", file=sys.stderr)
        return 1

    db = sqlite3.connect(OVERSEER_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    print("=== ensuring schema ===")
    db.executescript(SCHEMA_SQL)
    db.commit()

    print("\n=== parsing per-service HTML ===")
    total_events = 0
    for folder in sorted(base.iterdir()):
        if not folder.is_dir():
            continue
        service = SERVICE_MAP.get(folder.name)
        if not service:
            print(f"  skipping unknown folder: {folder.name}")
            continue
        path = folder / "MyActivity.html"
        if not path.is_file():
            continue
        events = parse_html(path, service)
        n = bulk_insert_events(db, events)
        db.commit()
        total_events += n
        print(f"  {service:15s} {n:>6} events")
    print(f"\n  TOTAL: {total_events} events inserted")

    print("\n=== computing daily rollups + novelty_score ===")
    n_rollups = compute_daily_rollups(db)
    print(f"  {n_rollups} (date, service) rollups computed")

    print("\n=== writing data_horizons row ===")
    upsert_horizon(db)
    db.commit()

    print("\n=== top-novelty days per service (sample) ===")
    for r in db.execute(
        """
        SELECT service, date, event_count, novelty_score
        FROM activity_daily
        WHERE novelty_score IS NOT NULL AND event_count >= 5
        ORDER BY novelty_score DESC LIMIT 10
        """
    ):
        print(
            f"  {r[0]:15s} {r[1]}  events={r[2]:>4}  "
            f"novelty={r[3]:.2f}"
        )

    print("\nDONE")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
