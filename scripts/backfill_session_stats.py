#!/usr/bin/env python3
"""Slice 4 CP1a backfill: compute extended stats (tokens, models,
file paths) for every existing imported_sessions row, then refresh
all project_summaries.

Run this ONCE on the Pi after deploying the CP1a code:

    ssh turfptax@10.0.0.25
    cd ~/cortex-core
    python3 scripts/backfill_session_stats.py

Idempotent — re-running skips rows that already have extended_stats_v
set in their metadata_json. Use --force to re-parse anyway (e.g. after
a parser fix).

Per-row work is bounded — for a session with N assistant messages the
parser does N json.loads + a small dict update. ~2-5ms per typical
Claude Code session, ~20-50ms for very long ones. 1000 sessions
backfill in well under a minute.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path


# Make the overseer plugin AND core src/ importable without installing.
# overseer_db.py imports cortex_db from src/ — both paths must be on
# sys.path when running this script directly via `python3 scripts/...`.
_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT / "plugins" / "overseer"))
sys.path.insert(0, str(_ROOT / "src"))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--db",
        default=os.environ.get(
            "OVERSEER_DB",
            str(Path.home() / "cortex-core" / "overseer.db"),
        ),
        help="Path to overseer.db (default: ~/cortex-core/overseer.db or "
             "$OVERSEER_DB).",
    )
    p.add_argument("--force", action="store_true",
                   help="Re-parse even rows that already have extended stats.")
    p.add_argument("--limit", type=int, default=0,
                   help="Process at most N rows (0 = all). For dry runs.")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("backfill")

    if not Path(args.db).is_file():
        print(
            "overseer.db not found at {} — pass --db or set OVERSEER_DB"
            .format(args.db),
            file=sys.stderr,
        )
        return 2

    # Imports deferred so the --help path doesn't try to load sqlite +
    # plugin code on a machine that doesn't have it.
    from overseer_db import OverseerDB  # type: ignore
    import project_summary  # type: ignore

    db = OverseerDB(args.db)
    log.info("opened %s", args.db)

    rows = db.list_imported_sessions(limit=10**9, offset=0)
    if args.limit:
        rows = rows[: args.limit]
    log.info("found %d imported_sessions rows to consider", len(rows))

    t0 = time.monotonic()
    n_skipped = 0
    n_updated = 0
    n_failed = 0
    failed_examples: list[tuple[str, str]] = []

    for i, row in enumerate(rows, 1):
        result = project_summary.refresh_session_extended_stats(
            db=db, imported_id=row["id"], force=args.force,
        )
        if not result.get("ok"):
            n_failed += 1
            if len(failed_examples) < 10:
                failed_examples.append(
                    (row["id"], result.get("error", "?")))
        elif result.get("updated"):
            n_updated += 1
        else:
            n_skipped += 1

        if i % 50 == 0:
            log.info(
                "progress %d / %d (updated=%d skipped=%d failed=%d)",
                i, len(rows), n_updated, n_skipped, n_failed,
            )

    parse_elapsed = time.monotonic() - t0
    log.info(
        "extended-stats pass complete: updated=%d skipped=%d failed=%d "
        "in %.1fs",
        n_updated, n_skipped, n_failed, parse_elapsed,
    )

    if failed_examples:
        log.warning("first failures:")
        for imp_id, err in failed_examples:
            log.warning("  %s: %s", imp_id, err)

    log.info("rolling up project_summaries…")
    t1 = time.monotonic()
    summary = project_summary.refresh_all_summaries(db)
    rollup_elapsed = time.monotonic() - t1
    log.info(
        "rollup complete: %d projects, %d refreshed, %d failed in %.1fs",
        summary["projects_total"], summary["refreshed"],
        summary["failed"], rollup_elapsed,
    )
    if summary.get("errors"):
        log.warning("rollup errors (first 10):")
        for e in summary["errors"]:
            log.warning("  %s", e)

    print(
        "BACKFILL DONE — {n_rows} rows ({u} updated, {s} skipped, "
        "{f} failed) + {p} projects rolled up. Total {t:.1f}s.".format(
            n_rows=len(rows), u=n_updated, s=n_skipped, f=n_failed,
            p=summary["projects_total"],
            t=parse_elapsed + rollup_elapsed,
        )
    )
    return 0 if (n_failed == 0 and summary["failed"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
