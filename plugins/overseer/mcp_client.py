"""Agent harness (2026-07-11): the Pi as MCP client (Option B).

Tory's decision: the overseer stays the SINGLE BRAIN with minimal
architectural change. Registered HTTP MCP servers (mcp_connectors
table) contribute tools directly into the overseer's existing chat
tool loop as mcp_<connector>_<tool>. The chat wire protocol does not
change; this module is additive to chat_tools.

Scope v1: streamable-HTTP MCP servers only (JSON-RPC over POST, plain
JSON or SSE-wrapped responses). Desktop-only stdio servers can be
bridged later by a thin HTTP proxy on the Hub; the proxy is plumbing,
not a second brain.

Failure posture: a connector must NEVER break the chat loop. Every
public function returns data or an {"error": ...} dict; nothing
raises across the boundary.
"""

import json
import logging
import re
import threading
import time

import requests

log = logging.getLogger("plugin.overseer.mcp")

PROTOCOL_VERSION = "2025-03-26"
INIT_TIMEOUT_S = 5
LIST_TIMEOUT_S = 5
CALL_TIMEOUT_S = 20
TOOLS_CACHE_TTL_S = 300
# A DOWN connector must not tax every chat turn: its error entry is
# cached too, but recovers faster than the happy-path TTL.
ERROR_CACHE_TTL_S = 120
MAX_RESPONSE_BYTES = 2_000_000
# Prompt-budget guard: external tools ride in the same tools array as
# the overseer's ~36 internal ones. Cap the external contribution.
MAX_MCP_TOOLS = 24

_lock = threading.Lock()
# name -> {"session": str|None, "protocol": str, "tools": [...],
#          "fetched_at": float, "config": (base_url, auth_header),
#          "error": str}
_cache: dict = {}
# full OpenAI tool name -> (connector_name, mcp_tool_name)
_name_map: dict = {}
# Single-flight: one fetch per connector at a time; concurrent chat
# turns wait for (and then reuse) the same refresh instead of
# stampeding a cold or dead server.
_fetch_locks: dict = {}


def _slug_ok(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name or ""))


def _rpc(conn: dict, method: str, params=None, *, rpc_id=1,
         session=None, protocol=None, timeout=CALL_TIMEOUT_S,
         notification=False) -> dict:
    """One JSON-RPC exchange with a WALL-CLOCK deadline.

    stream=True + incremental reads: requests' scalar timeout is
    time-between-bytes, so a server sending SSE keepalives would
    otherwise reset the clock forever and wedge the chat turn. We
    return as soon as the matching response event arrives instead of
    waiting for the server to close the stream (the spec says it
    SHOULD close, not MUST)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if conn.get("auth_header"):
        headers["Authorization"] = conn["auth_header"]
    if session:
        headers["Mcp-Session-Id"] = session
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not notification:
        body["id"] = rpc_id
    deadline = time.monotonic() + timeout
    resp = requests.post(conn["base_url"], json=body, headers=headers,
                         timeout=(min(5, timeout), timeout),
                         stream=True)
    try:
        if notification:
            return {"_status": resp.status_code,
                    "_session": resp.headers.get("mcp-session-id")}
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            parsed = None
            data_lines: list = []
            for line in resp.iter_lines(decode_unicode=True):
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"{method}: deadline exceeded mid-stream")
                if line is None:
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line == "" and data_lines:
                    # SSE event boundary: data lines join with \n.
                    raw = "\n".join(data_lines)
                    data_lines = []
                    try:
                        candidate = json.loads(raw)
                    except Exception:
                        continue
                    if (isinstance(candidate, dict)
                            and ("result" in candidate
                                 or "error" in candidate)
                            and candidate.get("id") == rpc_id):
                        parsed = candidate
                        break
            if parsed is None:
                raise ValueError("no JSON-RPC response in SSE stream")
        else:
            raw_bytes = b""
            for chunk in resp.iter_content(chunk_size=8192):
                raw_bytes += chunk
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"{method}: deadline exceeded reading body")
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    raise ValueError("response too large")
            parsed = json.loads(raw_bytes.decode("utf-8", "replace"))
        parsed["_session"] = resp.headers.get("mcp-session-id")
        return parsed
    finally:
        resp.close()


def _initialize(conn: dict) -> dict:
    """initialize + initialized-notification. Returns
    {"session": ..., "protocol": ...} or raises."""
    out = _rpc(conn, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "cortex-overseer", "version": "1.0"},
    }, timeout=INIT_TIMEOUT_S)
    if "error" in out:
        raise RuntimeError(f"initialize error: {out['error']}")
    session = out.get("_session")
    protocol = (out.get("result") or {}).get(
        "protocolVersion") or PROTOCOL_VERSION
    try:
        _rpc(conn, "notifications/initialized", {}, session=session,
             protocol=protocol, timeout=INIT_TIMEOUT_S,
             notification=True)
    except Exception as e:
        # Some servers do not require it; do not fail the handshake.
        log.debug("initialized notification failed (%s): %s",
                  conn.get("name"), e)
    return {"session": session, "protocol": protocol}


def _fetch_tools(conn: dict) -> dict:
    """Handshake + tools/list for one connector. Returns a cache
    entry. Raises on failure (caller stores the error)."""
    hs = _initialize(conn)
    out = _rpc(conn, "tools/list", {}, rpc_id=2,
               session=hs["session"], protocol=hs["protocol"],
               timeout=LIST_TIMEOUT_S)
    if "error" in out:
        raise RuntimeError(f"tools/list error: {out['error']}")
    tools = (out.get("result") or {}).get("tools") or []
    return {"session": hs["session"], "protocol": hs["protocol"],
            "tools": tools, "fetched_at": time.time(),
            "config": (conn["base_url"], conn.get("auth_header") or ""),
            "error": ""}


def _is_fresh(entry, conn, force) -> bool:
    if entry is None or force:
        return False
    if entry.get("config") != (conn["base_url"],
                               conn.get("auth_header") or ""):
        return False
    ttl = ERROR_CACHE_TTL_S if entry.get("error") else TOOLS_CACHE_TTL_S
    return (time.time() - entry.get("fetched_at", 0)) < ttl


def _entry_for(conn: dict, *, force=False) -> dict:
    """Cached entry for a connector, refreshed on TTL/config change.
    Single-flight per connector: concurrent chat turns wait for one
    refresh instead of each paying the handshake stall."""
    name = conn["name"]
    with _lock:
        entry = _cache.get(name)
        if _is_fresh(entry, conn, force):
            return entry
        flock = _fetch_locks.setdefault(name, threading.Lock())
    with flock:
        # Re-check: another thread may have refreshed while we waited.
        with _lock:
            entry = _cache.get(name)
            if _is_fresh(entry, conn, force):
                return entry
        try:
            entry = _fetch_tools(conn)
        except Exception as e:
            log.warning("mcp connector %s unavailable: %s", name, e)
            entry = {"session": None, "protocol": PROTOCOL_VERSION,
                     "tools": [], "fetched_at": time.time(),
                     "config": (conn["base_url"],
                                conn.get("auth_header") or ""),
                     "error": str(e)[:300]}
        with _lock:
            _cache[name] = entry
        return entry


def _safe_schema(schema) -> dict:
    """A malformed remote inputSchema would 400 the WHOLE tools array
    at the LLM endpoint, killing every chat turn for a cache TTL.
    Only accept an object schema; substitute an empty one otherwise."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    if schema.get("type") not in (None, "object"):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out.setdefault("type", "object")
    if "properties" in out and not isinstance(out["properties"], dict):
        out["properties"] = {}
    return out


def tool_definitions(db) -> list:
    """OpenAI-format tool defs for every enabled connector, capped at
    MAX_MCP_TOOLS. Also refreshes the reverse name map used by
    call_tool. Never raises.

    Name collisions (sanitization, dash folding, truncation) are
    detected: FIRST wins deterministically (connectors iterate in
    name order), later duplicates are skipped loudly. Duplicate
    function names in the tools array could 400 the endpoint and
    would silently misroute dispatch."""
    defs = []
    mapping = {}
    dropped_cap = 0
    try:
        connectors = db.list_mcp_connectors(enabled_only=True)
    except Exception as e:
        log.warning("mcp connector list failed: %s", e)
        return []
    for conn in connectors:
        entry = _entry_for(conn)
        for tool in entry["tools"]:
            raw = tool.get("name") or ""
            full = "mcp_{}_{}".format(
                conn["name"].replace("-", "_"),
                re.sub(r"[^a-zA-Z0-9_-]", "_", raw))[:64]
            if full in mapping:
                log.warning(
                    "mcp tool name collision: %s from connector %s "
                    "collides with %s/%s; skipping", full,
                    conn["name"], mapping[full][0], mapping[full][1])
                continue
            if len(defs) >= MAX_MCP_TOOLS:
                dropped_cap += 1
                continue
            mapping[full] = (conn["name"], raw)
            defs.append({
                "type": "function",
                "function": {
                    "name": full,
                    "description": ("[MCP connector '{}'] {}".format(
                        conn["name"],
                        (tool.get("description") or "")[:800])),
                    "parameters": _safe_schema(tool.get("inputSchema")),
                },
            })
    if dropped_cap:
        log.info("mcp tool cap (%d) reached; %d tool(s) dropped",
                 MAX_MCP_TOOLS, dropped_cap)
    with _lock:
        _name_map.clear()
        _name_map.update(mapping)
    return defs


def is_mcp_tool(name: str) -> bool:
    return (name or "").startswith("mcp_")


def call_tool(db, full_name: str, args: dict) -> dict:
    """Execute an MCP tool by its namespaced name. Returns the tool's
    text content or {"error": ...}. Never raises."""
    with _lock:
        target = _name_map.get(full_name)
    if not target:
        # Restart or stale map: refresh and retry the lookup once.
        tool_definitions(db)
        with _lock:
            target = _name_map.get(full_name)
    if not target:
        return {"error": f"unknown MCP tool: {full_name}"}
    conn_name, tool_name = target
    try:
        conns = {c["name"]: c
                 for c in db.list_mcp_connectors(enabled_only=True)}
    except Exception as e:
        return {"error": f"connector lookup failed: {e}"}
    conn = conns.get(conn_name)
    if not conn:
        return {"error": f"connector disabled or missing: {conn_name}"}

    def _do_call(entry):
        return _rpc(conn, "tools/call",
                    {"name": tool_name, "arguments": args or {}},
                    rpc_id=3, session=entry.get("session"),
                    protocol=entry.get("protocol"),
                    timeout=CALL_TIMEOUT_S)

    entry = _entry_for(conn)
    try:
        out = _do_call(entry)
    except requests.HTTPError as e:
        # Session likely expired: re-handshake once and retry.
        status = getattr(e.response, "status_code", 0)
        if status in (400, 404):
            entry = _entry_for(conn, force=True)
            try:
                out = _do_call(entry)
            except Exception as e2:
                return {"error": f"mcp call failed after re-init: "
                                 f"{e2}"[:300]}
        else:
            return {"error": f"mcp call failed: {e}"[:300]}
    except Exception as e:
        return {"error": f"mcp call failed: {e}"[:300]}

    if "error" in out:
        return {"error": str(out["error"])[:500]}
    result = out.get("result") or {}
    parts = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            parts.append(item.get("text") or "")
        else:
            parts.append(json.dumps(item, default=str)[:500])
    text = "\n".join(p for p in parts if p) or "(empty result)"
    if result.get("isError"):
        return {"error": text[:800]}
    return {"connector": conn_name, "tool": tool_name, "result": text}


def test_connector(db, name: str) -> dict:
    """Force a fresh handshake + tools/list for one connector. Used by
    the /mcp/connectors/test route."""
    conns = {c["name"]: c for c in db.list_mcp_connectors()}
    conn = conns.get((name or "").strip().lower())
    if not conn:
        return {"ok": False, "error": f"no such connector: {name}"}
    entry = _entry_for(conn, force=True)
    if entry["error"]:
        return {"ok": False, "error": entry["error"]}
    return {"ok": True,
            "tools": [t.get("name") for t in entry["tools"]],
            "count": len(entry["tools"])}


def validate_slug(name: str) -> bool:
    return _slug_ok((name or "").strip().lower())
