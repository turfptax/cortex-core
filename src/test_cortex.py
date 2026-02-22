#!/usr/bin/env python3
"""Standalone tests for Cortex DB and protocol handler.

Runs without BLE, display, or any hardware dependencies.
"""

import json
import os
import sys

# Allow running from the recorder directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cortex_db import CortexDB
from cortex_protocol import CortexProtocol, ChunkAssembler

DB_PATH = "/tmp/test_cortex.db"
PASSED = 0
FAILED = 0


def check(name, condition):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS: {name}")
    else:
        FAILED += 1
        print(f"  FAIL: {name}")


def test_db():
    print("\n=== CortexDB Tests ===")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    db = CortexDB(DB_PATH)

    # Insert note
    nid = db.insert_note("Test note", tags="test", project="cortex",
                         note_type="note", source="test")
    check("insert_note returns id", nid >= 1)

    # Insert voice note
    nid2 = db.insert_note("Voice test", source="voice", note_type="voice")
    check("insert voice note", nid2 == nid + 1)

    # Insert activity
    aid = db.insert_activity("vscode", details="editing", project="cortex")
    check("insert_activity returns id", aid >= 1)

    # Insert search
    sid = db.insert_search("python sqlite", source="google",
                           url="https://example.com", project="cortex")
    check("insert_search returns id", sid >= 1)

    # Start session
    sess_id = db.start_session(ai_platform="claude", hostname="TEST-PC",
                               os_info="Linux")
    check("start_session returns uuid", len(sess_id) == 36 and "-" in sess_id)

    # Insert note with session
    nid3 = db.insert_note("Session note", session_id=sess_id, project="cortex")
    check("insert_note with session_id", nid3 > nid2)

    # End session
    ok = db.end_session(sess_id, summary="Test session", projects="cortex")
    check("end_session returns True", ok is True)

    # End already-ended session
    ok2 = db.end_session(sess_id, summary="again")
    check("end_session already ended returns False", ok2 is False)

    # Upsert project
    tag = db.upsert_project("cortex", name="Cortex System", status="active")
    check("upsert_project returns tag", tag == "cortex")

    # Upsert again (update)
    tag2 = db.upsert_project("cortex", name="Cortex System v2")
    check("upsert_project update", tag2 == "cortex")

    # Register computer
    hn = db.register_computer("TEST-PC", os="Linux", cpu="ARM", ram_gb=0.5)
    check("register_computer returns hostname", hn == "TEST-PC")

    # Upsert person
    pid = db.upsert_person("tory", name="Tory Moghadam", role="Lead AI Engineer")
    check("upsert_person returns id", pid == "tory")

    # Stats
    stats = db.get_stats()
    check("stats notes_total", stats["notes_total"] == 3)
    check("stats activities_total", stats["activities_total"] == 1)
    check("stats searches_total", stats["searches_total"] == 1)
    check("stats sessions_total", stats["sessions_total"] == 1)
    check("stats active_sessions (0 after end)", stats["active_sessions"] == 0)

    # Recent notes
    notes = db.get_recent_notes(limit=5)
    check("get_recent_notes returns list", len(notes) == 3)
    check("notes ordered by created_at desc", notes[0]["id"] == nid3)

    # Recent notes filtered
    voice = db.get_recent_notes(note_type="voice")
    check("filter by note_type=voice", len(voice) == 1)

    # Recent sessions
    sessions = db.get_recent_sessions()
    check("get_recent_sessions", len(sessions) == 1)
    check("session has summary", sessions[0]["summary"] == "Test session")

    # Active projects
    projects = db.get_active_projects()
    check("get_active_projects", len(projects) == 1)

    # Get context
    ctx = db.get_context()
    check("get_context has keys", all(
        k in ctx for k in ("active_projects", "recent_sessions",
                           "recent_notes", "stats")
    ))

    db.close()
    os.remove(DB_PATH)
    print("  DB tests complete.")


def test_chunk_assembler():
    print("\n=== ChunkAssembler Tests ===")
    ca = ChunkAssembler()

    # Non-chunk passes through
    check("is_chunk false", not ChunkAssembler.is_chunk("CMD:ping"))
    check("is_chunk true", ChunkAssembler.is_chunk("CHUNK:1/2:data"))

    # Two-chunk reassembly
    result = ca.feed("CHUNK:1/2:CMD:note:{\"content\":\"first")
    check("chunk 1/2 returns None", result is None)
    result = ca.feed("CHUNK:2/2: part\"}")
    check("chunk 2/2 returns assembled", result == 'CMD:note:{"content":"first part"}')

    # Three-chunk reassembly
    ca.reset()
    r1 = ca.feed("CHUNK:1/3:aaa")
    r2 = ca.feed("CHUNK:2/3:bbb")
    r3 = ca.feed("CHUNK:3/3:ccc")
    check("3-chunk: first two None", r1 is None and r2 is None)
    check("3-chunk: third returns full", r3 == "aaabbbccc")

    # Out-of-order
    ca.reset()
    r1 = ca.feed("CHUNK:2/2:second")
    r2 = ca.feed("CHUNK:1/2:first")
    check("out-of-order reassembly", r2 == "firstsecond")

    # Invalid chunk format
    ca.reset()
    result = ca.feed("CHUNK:badformat")
    check("bad format returns None", result is None)

    print("  ChunkAssembler tests complete.")


def test_protocol():
    print("\n=== CortexProtocol Tests ===")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    db = CortexDB(DB_PATH)
    proto = CortexProtocol(db)
    ctx = {"app_state": "STT_IDLE", "uptime_s": 100.0,
           "disk_free_gb": 21.5, "ble_connected": True}

    # Ping
    resp = proto.handle_message("CMD:ping")
    check("CMD:ping -> RSP:pong", resp == "RSP:pong")

    # Status
    resp = proto.handle_message("CMD:status", context=ctx)
    check("CMD:status starts with RSP:status:", resp.startswith("RSP:status:"))
    status_json = json.loads(resp[11:])
    check("status has app_state", status_json["app_state"] == "STT_IDLE")
    check("status has notes_total", "notes_total" in status_json)

    # Note
    resp = proto.handle_message(
        'CMD:note:{"content":"Test note","tags":"test,cortex","project":"cortex","type":"bug"}'
    )
    check("CMD:note -> ACK:note:N", resp.startswith("ACK:note:"))
    note_id = int(resp.split(":")[-1])
    check("note id is positive", note_id >= 1)

    # Note missing content
    resp = proto.handle_message('CMD:note:{"tags":"test"}')
    check("note missing content -> ERR", resp.startswith("ERR:note:"))

    # Note invalid JSON
    resp = proto.handle_message("CMD:note:not json")
    check("note bad json -> ERR", resp.startswith("ERR:note:"))

    # Activity
    resp = proto.handle_message(
        'CMD:activity:{"program":"vscode","details":"coding","project":"cortex"}'
    )
    check("CMD:activity -> ACK:activity:N", resp.startswith("ACK:activity:"))

    # Activity missing program
    resp = proto.handle_message('CMD:activity:{"details":"test"}')
    check("activity missing program -> ERR", resp.startswith("ERR:activity:"))

    # Search
    resp = proto.handle_message(
        'CMD:search:{"query":"BLE MTU size","source":"google","project":"cortex"}'
    )
    check("CMD:search -> ACK:search:N", resp.startswith("ACK:search:"))

    # Session start
    resp = proto.handle_message(
        'CMD:session_start:{"ai_platform":"claude","hostname":"TEST","os_info":"Linux"}'
    )
    check("CMD:session_start -> ACK:session:UUID",
          resp.startswith("ACK:session:"))
    session_id = resp.split(":", 2)[-1]
    check("session id is UUID", len(session_id) == 36)
    check("active session tracked", proto.get_active_session_id() == session_id)

    # Note with active session auto-linked
    resp = proto.handle_message(
        'CMD:note:{"content":"Session note","project":"cortex"}'
    )
    check("note during session", resp.startswith("ACK:note:"))

    # Session end
    resp = proto.handle_message(
        f'CMD:session_end:{{"session_id":"{session_id}","summary":"Test done","projects":"cortex"}}'
    )
    check("CMD:session_end -> ACK", resp.startswith("ACK:session_end:"))
    check("active session cleared", proto.get_active_session_id() is None)

    # Session end with no active session
    resp = proto.handle_message("CMD:session_end:{}")
    check("session_end no session -> ERR", resp.startswith("ERR:session_end:"))

    # Get context
    resp = proto.handle_message("CMD:get_context")
    check("CMD:get_context -> RSP:context:", resp.startswith("RSP:context:"))
    ctx_data = json.loads(resp[12:])
    check("context has active_projects", "active_projects" in ctx_data)
    check("context has stats", "stats" in ctx_data)

    # Project upsert
    resp = proto.handle_message(
        'CMD:project_upsert:{"tag":"cortex","name":"Cortex System","status":"active"}'
    )
    check("CMD:project_upsert -> ACK:project:tag", resp == "ACK:project:cortex")

    # Computer register
    resp = proto.handle_message(
        'CMD:computer_reg:{"hostname":"TEST","os":"Linux","cpu":"ARM","ram_gb":0.5}'
    )
    check("CMD:computer_reg -> ACK:computer:hostname", resp == "ACK:computer:TEST")

    # People upsert
    resp = proto.handle_message(
        'CMD:people_upsert:{"id":"tory","name":"Tory Moghadam","role":"Lead AI Engineer"}'
    )
    check("CMD:people_upsert -> ACK:people:id", resp == "ACK:people:tory")

    # Query
    resp = proto.handle_message(
        'CMD:query:{"table":"notes","filters":{"project":"cortex"},"limit":5}'
    )
    check("CMD:query -> RSP:query:[...]", resp.startswith("RSP:query:"))
    results = json.loads(resp[10:])
    check("query returns list", isinstance(results, list))
    check("query results have content", len(results) > 0)

    # Query invalid table
    resp = proto.handle_message('CMD:query:{"table":"secret"}')
    check("query invalid table -> ERR", resp.startswith("ERR:query:"))

    # Unknown command
    resp = proto.handle_message("CMD:bogus")
    check("unknown command -> ERR", resp.startswith("ERR:bogus:unknown"))

    # Non-CMD message
    resp = proto.handle_message("just plain text")
    check("non-CMD returns None", resp is None)

    # Chunk reassembly + protocol
    resp = proto.handle_message('CHUNK:1/2:CMD:note:{"content":"chunked')
    check("chunk 1/2 returns None", resp is None)
    resp = proto.handle_message('CHUNK:2/2: note","project":"cortex"}')
    check("chunk 2/2 returns ACK", resp.startswith("ACK:note:"))

    # Outbound chunking
    long_resp = "RSP:status:" + json.dumps({"data": "x" * 600})
    chunks = proto.chunk_response(long_resp, max_size=200)
    check("long response chunked", len(chunks) > 1)
    check("chunks have CHUNK: prefix", all(c.startswith("CHUNK:") for c in chunks))

    # Short response not chunked
    short_chunks = proto.chunk_response("RSP:pong", max_size=200)
    check("short response not chunked", len(short_chunks) == 1)
    check("short response unchanged", short_chunks[0] == "RSP:pong")

    db.close()
    os.remove(DB_PATH)
    print("  Protocol tests complete.")


def main():
    global PASSED, FAILED
    print("Cortex Phase 1 — Test Suite")
    print("=" * 40)

    test_db()
    test_chunk_assembler()
    test_protocol()

    print("\n" + "=" * 40)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    if FAILED > 0:
        sys.exit(1)
    else:
        print("All tests passed!")


if __name__ == "__main__":
    main()
