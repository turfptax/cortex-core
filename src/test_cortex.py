#!/usr/bin/env python3
"""Standalone tests for Cortex DB and protocol handler.

Runs without BLE, display, or any hardware dependencies.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

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


def test_file_operations():
    print("\n=== File Operations Tests ===")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    db = CortexDB(DB_PATH)

    # --- DB layer ---

    # Insert file
    fid = db.insert_file("test.wav", category="recordings",
                         description="Test recording", tags="test,audio",
                         project="cortex", mime_type="audio/wav",
                         size_bytes=32000, source="test")
    check("insert_file returns id", fid >= 1)

    # Insert second file
    fid2 = db.insert_file("notes.txt", category="uploads",
                          description="Meeting notes", tags="meeting",
                          project="cortex", size_bytes=1024)
    check("insert second file", fid2 == fid + 1)

    # Insert file in different project
    fid3 = db.insert_file("data.csv", category="uploads",
                          description="Sensor data", tags="data",
                          project="bewell", size_bytes=2048)
    check("insert third file", fid3 == fid2 + 1)

    # List all files
    files = db.list_files()
    check("list_files returns all", len(files) == 3)

    # List by category
    recs = db.list_files(category="recordings")
    check("list_files filter category", len(recs) == 1)
    check("list_files category correct", recs[0]["filename"] == "test.wav")

    # List by project
    cortex_files = db.list_files(project="cortex")
    check("list_files filter project", len(cortex_files) == 2)

    # Search files
    results = db.search_files("meeting")
    check("search_files by description", len(results) == 1)
    check("search_files correct file", results[0]["filename"] == "notes.txt")

    results = db.search_files("audio")
    check("search_files by tags", len(results) == 1)

    results = db.search_files("data.csv")
    check("search_files by filename", len(results) == 1)

    # Delete file
    ok = db.delete_file(fid3)
    check("delete_file returns True", ok is True)
    files = db.list_files()
    check("list after delete", len(files) == 2)

    # Delete non-existent
    ok = db.delete_file(9999)
    check("delete non-existent returns False", ok is False)

    # Stats include files
    stats = db.get_stats()
    check("stats files_total", stats["files_total"] == 2)

    # Context includes recent_files
    ctx = db.get_context()
    check("context has recent_files", "recent_files" in ctx)
    check("context recent_files count", len(ctx["recent_files"]) == 2)

    # --- Protocol layer ---

    proto = CortexProtocol(db)

    # file_register
    resp = proto.handle_message(
        'CMD:file_register:{"filename":"proto.txt","category":"uploads",'
        '"description":"Protocol test","tags":"test","project":"cortex"}'
    )
    check("CMD:file_register -> ACK", resp.startswith("ACK:file_register:"))

    # file_register missing filename
    resp = proto.handle_message('CMD:file_register:{}')
    check("file_register no filename -> ERR", resp.startswith("ERR:file_register:"))

    # file_list
    resp = proto.handle_message('CMD:file_list:{"category":"uploads"}')
    check("CMD:file_list -> RSP", resp.startswith("RSP:file_list:"))
    file_list = json.loads(resp[14:])
    check("file_list returns list", isinstance(file_list, list))
    check("file_list has uploads", len(file_list) >= 2)

    # file_search
    resp = proto.handle_message('CMD:file_search:{"query":"Protocol"}')
    check("CMD:file_search -> RSP", resp.startswith("RSP:file_search:"))
    search_results = json.loads(resp[16:])
    check("file_search found file", len(search_results) == 1)

    # file_search missing query
    resp = proto.handle_message('CMD:file_search:{}')
    check("file_search no query -> ERR", resp.startswith("ERR:file_search:"))

    # file_delete
    file_id = int(resp.split(":")[-1]) if resp.startswith("ACK") else None
    # Use the proto.txt we just registered
    resp2 = proto.handle_message('CMD:file_list:{"limit":1}')
    last_file = json.loads(resp2[14:])[0]
    resp = proto.handle_message(
        'CMD:file_delete:{{"id":{}}}'.format(last_file["id"])
    )
    check("CMD:file_delete -> ACK", resp.startswith("ACK:file_delete:"))

    # file_delete missing id
    resp = proto.handle_message('CMD:file_delete:{}')
    check("file_delete no id -> ERR", resp.startswith("ERR:file_delete:"))

    # Query files table
    resp = proto.handle_message('CMD:query:{"table":"files","limit":10}')
    check("CMD:query files -> RSP", resp.startswith("RSP:query:"))
    query_results = json.loads(resp[10:])
    check("query files returns list", isinstance(query_results, list))

    db.close()
    os.remove(DB_PATH)
    print("  File operations tests complete.")


def test_wifi_protocol():
    print("\n=== WiFi Protocol Tests ===")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    db = CortexDB(DB_PATH)
    proto = CortexProtocol(db)

    # wifi_status — should return RSP with JSON (ip, hostname at minimum)
    resp = proto.handle_message("CMD:wifi_status")
    check("CMD:wifi_status -> RSP", resp.startswith("RSP:wifi_status:"))
    status = json.loads(resp[16:])
    check("wifi_status has hostname", "hostname" in status)

    # wifi_scan — should return RSP with JSON array (may be empty)
    resp = proto.handle_message("CMD:wifi_scan")
    check("CMD:wifi_scan -> RSP", resp.startswith("RSP:wifi_scan:"))
    networks = json.loads(resp[14:])
    check("wifi_scan returns list", isinstance(networks, list))
    if networks:
        check("wifi_scan has ssid field", "ssid" in networks[0])
    else:
        check("wifi_scan empty list ok", True)

    # wifi_config — missing ssid
    resp = proto.handle_message('CMD:wifi_config:{}')
    check("wifi_config no ssid -> ERR", resp == "ERR:wifi_config:missing ssid")

    # wifi_config — missing payload
    resp = proto.handle_message('CMD:wifi_config')
    check("wifi_config no payload -> ERR or handles gracefully",
          resp.startswith("ERR:") or resp.startswith("RSP:"))

    db.close()
    os.remove(DB_PATH)
    print("  WiFi protocol tests complete.")


def test_http_server():
    print("\n=== HTTP Server Tests ===")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    # Create temp directories for file serving
    tmp_dir = tempfile.mkdtemp(prefix="cortex_test_")
    uploads_dir = os.path.join(tmp_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    # Patch config values for test
    import config
    orig_uploads = config.UPLOADS_DIR
    orig_token_path = config.HTTP_TOKEN_PATH
    orig_db_path = config.CORTEX_DB_PATH
    config.UPLOADS_DIR = uploads_dir
    config.HTTP_TOKEN_PATH = os.path.join(tmp_dir, "test-token")
    config.CORTEX_DB_PATH = DB_PATH

    try:
        from http_server import CortexHTTPServer, CortexHTTPHandler, _FILE_DIRS
        # Also patch the module-level _FILE_DIRS
        orig_file_dirs = dict(_FILE_DIRS)
        _FILE_DIRS["uploads"] = uploads_dir

        db = CortexDB(DB_PATH)
        proto = CortexProtocol(db)

        # Create test token
        token = "test-token-12345"
        with open(config.HTTP_TOKEN_PATH, "w") as f:
            f.write(token)

        # Start server on random port
        server = CortexHTTPServer(
            ("127.0.0.1", 0), CortexHTTPHandler, proto, None, token
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = "http://127.0.0.1:{}".format(port)

        # Helper for requests
        def http_get(path, auth=True):
            req = urllib.request.Request(base_url + path)
            if auth:
                req.add_header("Authorization", "Bearer " + token)
            return urllib.request.urlopen(req, timeout=5)

        def http_post(path, body, auth=True, headers=None):
            data = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
            req = urllib.request.Request(base_url + path, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            if auth:
                req.add_header("Authorization", "Bearer " + token)
            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)
            return urllib.request.urlopen(req, timeout=5)

        # GET /health — no auth required
        resp = http_get("/health", auth=False)
        check("GET /health -> 200", resp.status == 200)
        health = json.loads(resp.read())
        check("health has ok=True", health["ok"] is True)

        # POST /api/cmd without auth -> 401
        try:
            http_post("/api/cmd", {"command": "ping"}, auth=False)
            check("POST /api/cmd no auth -> 401", False)
        except urllib.error.HTTPError as e:
            check("POST /api/cmd no auth -> 401", e.code == 401)

        # POST /api/cmd with auth — ping
        resp = http_post("/api/cmd", {"command": "ping"})
        check("POST /api/cmd -> 200", resp.status == 200)
        result = json.loads(resp.read())
        check("cmd ping ok=True", result["ok"] is True)
        check("cmd ping response=RSP:pong", result["response"] == "RSP:pong")

        # POST /api/cmd — note
        resp = http_post("/api/cmd", {
            "command": "note",
            "payload": {"content": "HTTP test note", "project": "test"}
        })
        result = json.loads(resp.read())
        check("cmd note via HTTP", result["response"].startswith("ACK:note:"))

        # POST /api/cmd — status
        resp = http_post("/api/cmd", {"command": "status"})
        result = json.loads(resp.read())
        check("cmd status via HTTP", result["response"].startswith("RSP:status:"))

        # GET /files/uploads — list (empty)
        resp = http_get("/files/uploads")
        check("GET /files/uploads -> 200", resp.status == 200)
        file_resp = json.loads(resp.read())
        check("files list ok=True", file_resp.get("ok") is True)
        check("files list has files array", isinstance(file_resp.get("files"), list))

        # Upload a test file
        test_content = b"Hello from HTTP test!"
        req = urllib.request.Request(
            base_url + "/files/uploads",
            data=test_content,
            method="POST",
        )
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("X-Filename", "http_test.txt")
        req.add_header("Content-Length", str(len(test_content)))
        resp = urllib.request.urlopen(req, timeout=5)
        check("upload file -> 200", resp.status == 200)
        upload_result = json.loads(resp.read())
        check("upload ok=True", upload_result["ok"] is True)

        # Download the file
        resp = http_get("/files/uploads/http_test.txt")
        check("download file -> 200", resp.status == 200)
        downloaded = resp.read()
        check("download content matches", downloaded == test_content)

        # List files again — should have 1
        resp = http_get("/files/uploads")
        file_resp = json.loads(resp.read())
        file_list = file_resp.get("files", [])
        check("files list has upload", len(file_list) >= 1)
        check("file list has name", any(f["name"] == "http_test.txt" for f in file_list))

        # GET /files/db — download database snapshot
        resp = http_get("/files/db")
        check("GET /files/db -> 200", resp.status == 200)
        db_bytes = resp.read()
        check("db download has content", len(db_bytes) > 0)
        check("db download is sqlite", db_bytes[:6] == b"SQLite")

        # Cleanup
        server.shutdown()
        db.close()
        os.remove(DB_PATH)

    finally:
        # Restore config
        config.UPLOADS_DIR = orig_uploads
        config.HTTP_TOKEN_PATH = orig_token_path
        config.CORTEX_DB_PATH = orig_db_path
        _FILE_DIRS.update(orig_file_dirs)
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("  HTTP server tests complete.")


def main():
    global PASSED, FAILED
    print("Cortex — Test Suite")
    print("=" * 40)

    test_db()
    test_chunk_assembler()
    test_protocol()
    test_file_operations()
    test_wifi_protocol()
    test_http_server()

    print("\n" + "=" * 40)
    print(f"Results: {PASSED} passed, {FAILED} failed")
    if FAILED > 0:
        sys.exit(1)
    else:
        print("All tests passed!")


if __name__ == "__main__":
    main()
