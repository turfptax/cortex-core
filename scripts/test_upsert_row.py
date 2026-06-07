"""Regression test for CortexDB.upsert_row partial-update semantics.

This test was added 2026-06-06 after the looper (iter #2) flagged
that note_update was silently destroying note content. Root cause:
upsert_row used SQLite INSERT OR REPLACE, which DELETE-then-INSERTs
the row, NULLing every column not in the partial dict.

The fix distinguishes UPDATE (partial-safe) from INSERT (new row).
This test exercises both paths plus the no-op + new-row-with-PK
cases so a future refactor can't silently regress note_update again.

Run:
  python scripts/test_upsert_row.py            # against an in-memory DB
  python scripts/test_upsert_row.py --pi       # smoke-test against .25
                                                 (read-only — uses a
                                                  scratch note that gets
                                                  deleted at the end)
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cortex_db import CortexDB  # noqa: E402


class TestFailed(AssertionError):
    pass


def _assert(cond, msg):
    if not cond:
        raise TestFailed(msg)


def run_local_tests():
    """Exercise upsert_row against an empty temp DB."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        db = CortexDB(str(db_path))
        try:
            _run_local_test_body(db)
        finally:
            try:
                db.close()
            except Exception:
                pass


def _run_local_test_body(db):

        # ── Test 1: INSERT a note (auto-PK, no id supplied) ──────
        note_id = db.upsert_row("notes", {
            "content": "Original content",
            "note_type": "note",
            "tags": "test,original",
            "project": "test-proj",
        })
        _assert(isinstance(note_id, int) and note_id > 0,
                f"insert returned bad id: {note_id!r}")
        row = db._conn.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        _assert(row is not None, "row missing after insert")
        _assert(dict(row).get("content") == "Original content",
                "content not stored on insert")
        print(f"  [OK]INSERT new note id={note_id}")

        # ── Test 2: partial UPDATE (THE REGRESSION) ──────────────
        # This is what note_update calls: id + tags only. Content
        # MUST be preserved.
        db.upsert_row("notes", {
            "id": note_id,
            "tags": "updated,tags",
        })
        row = dict(db._conn.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone())
        _assert(row["content"] == "Original content",
                f"REGRESSION: partial update destroyed content "
                f"(content={row['content']!r})")
        _assert(row["tags"] == "updated,tags",
                f"partial update did not apply tags "
                f"(tags={row['tags']!r})")
        _assert(row["note_type"] == "note",
                f"note_type clobbered by partial update "
                f"(note_type={row['note_type']!r})")
        _assert(row["project"] == "test-proj",
                f"project clobbered by partial update "
                f"(project={row['project']!r})")
        print("  [OK]partial UPDATE preserves untouched columns")

        # ── Test 3: partial UPDATE of project only ───────────────
        db.upsert_row("notes", {
            "id": note_id,
            "project": "different-proj",
        })
        row = dict(db._conn.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone())
        _assert(row["content"] == "Original content",
                "content lost on project-only update")
        _assert(row["tags"] == "updated,tags",
                "tags lost on project-only update")
        _assert(row["project"] == "different-proj",
                "project not applied")
        print("  [OK]project-only UPDATE preserves content + tags")

        # ── Test 4: UPDATE of all three triage fields at once ────
        db.upsert_row("notes", {
            "id": note_id,
            "tags": "final,tags",
            "project": "final-proj",
            "note_type": "decision",
        })
        row = dict(db._conn.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone())
        _assert(row["content"] == "Original content",
                "content lost on triage-fields update")
        _assert(row["tags"] == "final,tags", "tags not applied")
        _assert(row["project"] == "final-proj", "project not applied")
        _assert(row["note_type"] == "decision", "note_type not applied")
        print("  [OK]triage UPDATE (tags + project + note_type) works")

        # ── Test 5: no-op — only PK supplied ─────────────────────
        result = db.upsert_row("notes", {"id": note_id})
        _assert(result == note_id, "no-op should return PK")
        row = dict(db._conn.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone())
        _assert(row["content"] == "Original content",
                "no-op destroyed content")
        print("  [OK]no-op (PK only) preserves row")

        # ── Test 6: INSERT with explicit PK (new row) ────────────
        new_id = 999999
        result = db.upsert_row("notes", {
            "id": new_id,
            "content": "Explicit PK row",
            "note_type": "context",
        })
        _assert(result == new_id, "explicit-PK INSERT didn't return PK")
        row = dict(db._conn.execute(
            "SELECT * FROM notes WHERE id = ?", (new_id,)
        ).fetchone())
        _assert(row is not None, "explicit-PK INSERT didn't land")
        _assert(row["content"] == "Explicit PK row",
                "explicit-PK INSERT content wrong")
        print(f"  [OK]INSERT with explicit PK id={new_id}")

        # ── Test 7: keyed table (projects, pk=tag) UPDATE ────────
        db.upsert_row("projects", {
            "tag": "test-project",
            "name": "Test Project",
            "status": "active",
            "description": "Original description",
        })
        db.upsert_row("projects", {
            "tag": "test-project",
            "status": "paused",
        })
        row = dict(db._conn.execute(
            "SELECT * FROM projects WHERE tag = ?",
            ("test-project",),
        ).fetchone())
        _assert(row["name"] == "Test Project",
                "project name lost on partial update")
        _assert(row["description"] == "Original description",
                "project description lost on partial update")
        _assert(row["status"] == "paused", "project status not applied")
        print("  [OK]keyed table (projects) partial UPDATE works")

        db.close()


def run_pi_smoke_test():
    """Round-trip a scratch note through the actual Pi MCP path to
    confirm the deployed fix works end-to-end. Creates a note,
    triages it via the same code path note_update uses, verifies
    content survives, then deletes the scratch note."""
    import json
    import urllib.request
    import urllib.parse
    import base64

    pi = "http://10.0.0.25:8420"
    auth = "Basic " + base64.b64encode(b"cortex:cortex").decode()

    def post(cmd, payload):
        # The HTTP server's _handle_cmd does its own json.dumps on
        # `payload` when building the protocol message — pass the
        # dict directly, NOT a pre-encoded string, or you double-
        # encode and the handler's json.loads returns a string.
        body = json.dumps(
            {"command": cmd, "payload": payload}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{pi}/api/cmd", data=body,
            headers={"Authorization": auth,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def query(filters):
        return post("query", {
            "table": "notes", "filters": filters,
            "limit": 5, "order_by": "id DESC",
        })

    # 1. Create scratch note
    print("  creating scratch note...")
    sentinel = "UPSERT-REGRESSION-TEST-2026-06-06"
    create = post("note", {
        "content": sentinel,
        "note_type": "note",
        "tags": "regression-test,scratch",
    })
    print(f"    create response: {create}")

    # 2. Find its id
    q = query({"tags": "regression-test,scratch"})
    print(f"    query response: {q}")
    raw = q.get("response", "")
    if raw.startswith("RSP:query:"):
        rows = json.loads(raw[len("RSP:query:"):])
    else:
        raise SystemExit(f"unexpected query response: {raw[:200]}")
    matching = [r for r in rows if r.get("content") == sentinel]
    if not matching:
        raise SystemExit("scratch note not found after creation")
    note_id = matching[0]["id"]
    print(f"    scratch note id={note_id}")

    # 3. Partial-update via upsert (the THE bug path)
    print("  partial-updating via upsert (the bug path)...")
    upd = post("upsert", {
        "table": "notes",
        "data": {"id": note_id, "tags": "REGRESSION-UPDATED"},
    })
    print(f"    upsert response: {upd}")

    # 4. Re-fetch and verify content survived
    q2 = query({"id": note_id})
    raw2 = q2.get("response", "")
    if raw2.startswith("RSP:query:"):
        rows2 = json.loads(raw2[len("RSP:query:"):])
    else:
        raise SystemExit(f"unexpected query response: {raw2[:200]}")
    if not rows2:
        raise SystemExit(
            f"FAIL: note id={note_id} VANISHED after partial upsert"
        )
    after = rows2[0]
    if after.get("content") != sentinel:
        raise SystemExit(
            f"FAIL: content destroyed after partial upsert. "
            f"Got: {after.get('content')!r}"
        )
    if after.get("tags") != "REGRESSION-UPDATED":
        raise SystemExit(
            f"FAIL: tags not applied. Got: {after.get('tags')!r}"
        )
    print("    [OK]content preserved, tags applied")

    # 5. Clean up
    print(f"  cleaning up scratch note id={note_id}...")
    post("delete", {"table": "notes", "row_id": note_id})
    print("  [OK]Pi smoke test passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pi", action="store_true",
        help="Also run the live smoke test against turfptax@10.0.0.25",
    )
    args = parser.parse_args()

    print("=== local upsert_row tests ===")
    try:
        run_local_tests()
        print("  ALL LOCAL TESTS PASSED")
    except TestFailed as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    if args.pi:
        print()
        print("=== Pi smoke test against .25 ===")
        try:
            run_pi_smoke_test()
        except Exception as e:
            print(f"  FAIL: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
