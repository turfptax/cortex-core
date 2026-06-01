"""Bulk-import Google Recorder ZIP exports into Cortex human journal.

Google Recorder exports a recording as a single .zip containing:
  - <name>.m4a   the audio
  - <name>.txt   the auto-transcribed text (speaker-labeled)

The filename pattern is "<Mon> <D> at <HH-MM>" — e.g. "Jun 1 at 11-22".
This script:
  1. Walks a directory for .zip files matching that pattern
  2. Extracts the .txt content
  3. Parses the filename for date + time → ISO local timestamp
  4. POSTs each transcript to /plugins/overseer/human-journal with
     entry_type=voice and local_created_at set to the recording moment
     (NOT import time)
  5. Audio (.m4a) is left in place for future re-transcription needs;
     the audio archive is the user's filesystem of record for now.

Idempotency:
  Best-effort dedup — checks recent human_journal_entries for a match
  on (local_created_at, first 200 chars of text). Skips re-import.

Usage:
  python import_recorder_zips.py --dir "C:/path/to/recorder google/" \\
                                 --pi http://10.0.0.25:8420 \\
                                 --user cortex --pass cortex \\
                                 --year 2026

The --year arg disambiguates filenames (Google Recorder doesn't
include the year). Defaults to current year.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


# Filename pattern: "Jun 1 at 11-22" — month abbrev + day + "at" + HH-MM
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_NAME_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+at\s+"
    r"(?P<hh>\d{1,2})-(?P<mm>\d{2})$"
)


def parse_recorder_filename(stem: str, *, year: int,
                              tz_offset: str = "-05:00") -> str | None:
    """Parse 'Jun 1 at 11-22' → ISO local timestamp with offset.

    Returns None if the stem doesn't match. Caller passes year because
    the filename doesn't carry it.
    """
    m = _NAME_RE.match(stem.strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group("mon"))
    if not mon:
        return None
    day = int(m.group("day"))
    hh = int(m.group("hh"))
    mm = int(m.group("mm"))
    return (f"{year:04d}-{mon:02d}-{day:02d}T"
            f"{hh:02d}:{mm:02d}:00{tz_offset}")


def extract_zip(zip_path: Path, out_dir: Path) -> tuple[Path | None,
                                                          Path | None]:
    """Extract zip into out_dir. Returns (txt_path, m4a_path).
    Either may be None if the archive doesn't contain that file type.
    """
    txt_path = m4a_path = None
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.infolist():
            name = member.filename
            if name.endswith("/") or name.startswith(".."):
                continue
            z.extract(member, out_dir)
            if name.lower().endswith(".txt"):
                txt_path = out_dir / name
            elif name.lower().endswith(".m4a"):
                m4a_path = out_dir / name
    return txt_path, m4a_path


def _auth_header(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def http_get_json(url: str, user: str, password: str,
                   timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": _auth_header(user, password),
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url: str, user: str, password: str, body: dict,
                    timeout: float = 30.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": _auth_header(user, password),
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def already_imported(pi: str, user: str, password: str,
                       local_created_at: str,
                       text_head: str) -> bool:
    """Cheap dedup: look at the most recent N human-journal entries
    and check if any match (local_created_at, first 200 chars of text).
    """
    try:
        resp = http_get_json(
            f"{pi}/plugins/overseer/human-journal?limit=200",
            user, password,
        )
    except Exception:
        return False
    if not resp.get("ok"):
        return False
    head = text_head.strip()[:200]
    target_date = local_created_at[:10]
    for e in resp.get("entries", []):
        if (e.get("local_created_at") or "").startswith(target_date):
            existing = (e.get("text") or "").strip()[:200]
            if existing == head:
                return True
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir", required=True,
        help="Directory containing Google Recorder .zip files",
    )
    parser.add_argument(
        "--pi", default="http://10.0.0.25:8420",
        help="Cortex Pi HTTP base URL",
    )
    parser.add_argument("--user", default="cortex")
    parser.add_argument("--password", default="cortex")
    parser.add_argument(
        "--year", type=int, default=_dt.datetime.now().year,
        help=("Year for filename timestamps — Google Recorder omits "
              "it. Defaults to current year."),
    )
    parser.add_argument(
        "--tz-offset", default="-05:00",
        help="Local TZ offset to embed in the timestamp (default CDT)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse + print intended POSTs without sending",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip dedup check; import even if a match is found",
    )
    args = parser.parse_args(argv)

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        print(f"ERR: not a directory: {root}", file=sys.stderr)
        return 2

    extract_dir = root / "extracted"
    extract_dir.mkdir(exist_ok=True)

    zips = sorted(root.glob("*.zip"))
    if not zips:
        print(f"no .zip files in {root}", file=sys.stderr)
        return 1

    results = []
    for z in zips:
        stem = z.stem
        ts = parse_recorder_filename(
            stem, year=args.year, tz_offset=args.tz_offset)
        if not ts:
            results.append({"zip": z.name, "ok": False,
                            "error": "filename did not parse"})
            continue
        try:
            txt_path, m4a_path = extract_zip(z, extract_dir)
        except Exception as e:
            results.append({"zip": z.name, "ok": False,
                            "error": f"extract failed: {e}"})
            continue
        if not txt_path:
            results.append({"zip": z.name, "ok": False,
                            "error": "no .txt in archive"})
            continue
        text = txt_path.read_text(encoding="utf-8").strip()
        if not text:
            results.append({"zip": z.name, "ok": False,
                            "error": "empty transcript"})
            continue

        intent = {
            "zip": z.name,
            "local_created_at": ts,
            "char_count": len(text),
            "audio_kept": (str(m4a_path) if m4a_path else None),
        }

        if args.dry_run:
            intent["status"] = "dry-run"
            intent["text_head"] = text[:120]
            results.append(intent)
            continue

        if not args.force and already_imported(
                args.pi, args.user, args.password, ts, text):
            intent["status"] = "skipped-duplicate"
            results.append(intent)
            continue

        try:
            resp = http_post_json(
                f"{args.pi}/plugins/overseer/human-journal",
                args.user, args.password,
                {
                    "text": text,
                    "entry_type": "voice",
                    "local_created_at": ts,
                },
                timeout=60.0,
            )
            if resp.get("ok"):
                intent["status"] = "imported"
                intent["journal_id"] = resp.get("id")
            else:
                intent["status"] = "error"
                intent["error"] = resp.get("error")
        except urllib.error.HTTPError as e:
            intent["status"] = "error"
            intent["error"] = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
        except Exception as e:
            intent["status"] = "error"
            intent["error"] = str(e)
        results.append(intent)

    # Report
    print(json.dumps({
        "root": str(root),
        "year": args.year,
        "dry_run": args.dry_run,
        "results": results,
        "summary": {
            "imported": sum(1 for r in results
                            if r.get("status") == "imported"),
            "skipped": sum(1 for r in results
                           if r.get("status") == "skipped-duplicate"),
            "error":    sum(1 for r in results
                            if r.get("status") == "error"),
            "dry_run":  sum(1 for r in results
                            if r.get("status") == "dry-run"),
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
