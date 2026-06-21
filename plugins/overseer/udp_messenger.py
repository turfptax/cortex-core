#!/usr/bin/env python3
"""Cortex overseer <-> OpenClaw agents over UDP.

A faithful Python port of Tory's openclaw-udp-messenger v1.6.x wire protocol
(github.com/turfptax/openclaw-udp-messenger) so the Cortex overseer can be a
first-class peer on the agent LAN alongside Wol (the OpenClaw agent on
10.0.0.195).

Protocol (every packet is JSON, UTF-8, one datagram):
  magic     : "CLAUDE-UDP-V1"   (drop anything else)
  port      : UDP 51337         (agent-to-agent)
  timestamp : epoch milliseconds (Date.now())
  types     : discovery-ping / discovery-pong / message
    ping/pong : {magic,type,sender_id,sender_port,timestamp}
    message   : {magic,type,sender_id,sender_port,payload,timestamp}
  agent_id  : "<hostname>-<sha256(hostname:sortedMACs:port)[:8]>" (stable)
  trust     : approve-once. Trusted peers (by agent_id, or hostname-prefix)
              get their messages flagged trusted; the agent wakes only on
              trusted messages. Pre-seed both sides' trust to skip approval.

Standalone CLI (run on the Cortex Pi 10.0.0.25):
    python3 udp_messenger.py whoami
    python3 udp_messenger.py discover
    python3 udp_messenger.py send 10.0.0.195:51337 "hello from the overseer"
    python3 udp_messenger.py listen            # foreground receive loop
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time

MAGIC = "CLAUDE-UDP-V1"
PORT = 51337
MAX_MESSAGE_SIZE = 4096
DISCOVERY_WAIT_S = 2.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _local_macs() -> list[str]:
    """Non-internal MACs, sorted (matches the TS os.networkInterfaces() order)."""
    macs = set()
    try:
        base = "/sys/class/net"
        for nic in os.listdir(base):
            if nic == "lo":
                continue
            try:
                mac = open(os.path.join(base, nic, "address")).read().strip()
            except OSError:
                continue
            if mac and mac != "00:00:00:00:00:00":
                macs.add(mac)
    except OSError:
        pass
    return sorted(macs)


def stable_agent_id() -> str:
    host = socket.gethostname()
    macs = _local_macs()
    seed = f"{host}:{','.join(macs)}:{PORT}" if macs else f"{host}:{PORT}"
    h = hashlib.sha256(seed.encode()).hexdigest()[:8]
    return f"{host}-{h}"


def _broadcast_addrs() -> list[str]:
    outs = set(["255.255.255.255"])
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        parts = ip.split(".")
        parts[3] = "255"
        outs.add(".".join(parts))
    except OSError:
        pass
    return sorted(outs)


def _hostname_prefix(agent_id: str) -> str:
    return agent_id.rsplit("-", 1)[0] if "-" in agent_id else agent_id


class Messenger:
    """Bind 51337, answer pings, collect pongs, queue inbound messages.

    trusted: iterable of agent_ids we trust (exact or hostname-prefix). on_message
    is called for every inbound message (dict with from/fromId/message/trusted)."""

    def __init__(self, trusted=None, on_message=None, relay=("10.0.0.195", 31415)):
        self.agent_id = stable_agent_id()
        self.trusted = set(trusted or [])
        self.on_message = on_message
        # Relay monitor (the dashboard). Mirror every sent/received event here
        # so the human operator sees the overseer's traffic. None disables it.
        self.relay_addr = tuple(relay) if relay else None
        self.inbox: list[dict] = []
        self.discovered: list[dict] = []
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("0.0.0.0", PORT))
        self.sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def _relay(self, event: str, peer_id, peer_address: str, payload: str):
        """Forward a sent/received/system event to the relay monitor so it
        shows on the dashboard. Best-effort; never breaks the real path."""
        if not self.relay_addr:
            return
        try:
            pkt = json.dumps({
                "magic": MAGIC, "type": "relay", "relay_event": event,
                "agent_id": self.agent_id, "peer_id": peer_id or "unknown",
                "peer_address": peer_address, "payload": payload,
                "timestamp": _now_ms(),
            }).encode()
            self.sock.sendto(pkt, self.relay_addr)
        except OSError:
            pass

    def is_trusted(self, peer_id: str) -> bool:
        if peer_id in self.trusted:
            return True
        host = _hostname_prefix(peer_id)
        return any(_hostname_prefix(t) == host for t in self.trusted)

    def _listen(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("magic") != MAGIC or msg.get("sender_id") == self.agent_id:
                continue
            pid = msg.get("sender_id")
            paddr = f"{addr[0]}:{msg.get('sender_port') or addr[1]}"
            mtype = msg.get("type")

            if mtype == "discovery-ping":
                reply = json.dumps({
                    "magic": MAGIC, "type": "discovery-pong",
                    "sender_id": self.agent_id, "sender_port": PORT,
                    "timestamp": _now_ms(),
                }).encode()
                try:
                    self.sock.sendto(reply, addr)
                except OSError:
                    pass
            elif mtype == "discovery-pong":
                self.discovered.append({"id": pid, "address": paddr})
            elif mtype == "message":
                payload = msg.get("payload")
                payload = payload if isinstance(payload, str) else str(payload or "")
                if len(payload) > MAX_MESSAGE_SIZE:
                    payload = payload[:MAX_MESSAGE_SIZE] + " ... [truncated]"
                rec = {
                    "from": paddr, "fromId": pid, "message": payload,
                    "timestamp": msg.get("timestamp") or _now_ms(),
                    "trusted": self.is_trusted(pid),
                }
                self.inbox.append(rec)
                self._relay("received", pid, paddr, payload)
                if self.on_message:
                    try:
                        self.on_message(rec)
                    except Exception:
                        pass

    def discover(self, wait: float = DISCOVERY_WAIT_S) -> list[dict]:
        self.discovered = []
        ping = json.dumps({
            "magic": MAGIC, "type": "discovery-ping",
            "sender_id": self.agent_id, "sender_port": PORT,
            "timestamp": _now_ms(),
        }).encode()
        for b in _broadcast_addrs():
            try:
                self.sock.sendto(ping, (b, PORT))
            except OSError:
                pass
        time.sleep(wait)
        # de-dup by id
        seen, out = set(), []
        for d in self.discovered:
            if d["id"] not in seen:
                seen.add(d["id"])
                out.append(d)
        return out

    def send(self, ip: str, port: int, text: str, peer_id=None):
        pkt = json.dumps({
            "magic": MAGIC, "type": "message",
            "sender_id": self.agent_id, "sender_port": PORT,
            "payload": text, "timestamp": _now_ms(),
        }).encode()
        self.sock.sendto(pkt, (ip, int(port)))
        self._relay("sent", peer_id, f"{ip}:{int(port)}", text)

    def drain_inbox(self) -> list[dict]:
        msgs, self.inbox = self.inbox, []
        return msgs

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def _cli(argv):
    try:
        sys.stdout.reconfigure(line_buffering=True)  # flush per line even to a file
    except Exception:
        pass
    cmd = argv[1] if len(argv) > 1 else "whoami"
    if cmd == "whoami":
        print(stable_agent_id())
        return 0
    m = Messenger()
    try:
        if cmd == "discover":
            print(f"agent_id: {m.agent_id}")
            found = m.discover()
            if not found:
                print("no agents found")
            for d in found:
                print(f"  {d['id']} @ {d['address']}")
        elif cmd == "send" and len(argv) >= 4:
            host, _, port = argv[2].partition(":")
            m.send(host, int(port or PORT), argv[3])
            print(f"sent to {host}:{port or PORT} as {m.agent_id}")
            time.sleep(1.0)  # let any immediate reply land
            for r in m.drain_inbox():
                print(f"  [reply] {r['fromId']}: {r['message'][:200]}")
        elif cmd == "listen":
            print(f"listening on :{PORT} as {m.agent_id} (Ctrl-C to stop)")
            while True:
                time.sleep(2)
                for r in m.drain_inbox():
                    tag = "trusted" if r["trusted"] else "UNTRUSTED"
                    print(f"  [{tag}] {r['fromId']} @ {r['from']}: {r['message'][:300]}")
        else:
            print(__doc__)
            return 2
    finally:
        if cmd != "listen":
            m.stop()
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
