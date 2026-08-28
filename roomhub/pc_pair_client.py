#!/usr/bin/env python3
"""
PC-side companion for the IRA room hub on your Mac.

What it does
------------
1. Waits for your approval inside IRA (the Mac shows a pairing prompt).
2. Completes the brahma_connect handshake (hello -> pair_request -> pair_approved).
3. Keeps a live WebSocket to the Mac so you can send text commands from THIS PC
   and read back status / device list / logs.

The Mac must already be running its brahma gateway:
    python roomhub/mac_roomhub_launcher.py
(or `python -m brahma_connect.service` style start) with host=<MAC_LAN_IP>
and a non-empty admin_token.

Usage:
    python pc_pair_client.py --host 192.168.x.x --port 8765 --token YOUR_ADMIN_TOKEN
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

try:
    import websocket  # websocket-client
except ImportError:
    sys.exit("Missing dep: pip install websocket-client")


def _msg(msg_type: str, payload: dict | None = None) -> str:
    return json.dumps({
        "type": msg_type,
        "request_id": uuid.uuid4().hex,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload or {},
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="Mac LAN IP")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", required=True, help="admin_token configured on the Mac gateway")
    ap.add_argument("--name", default="Windows-PC", help="device name shown on the Mac")
    args = ap.parse_args()

    url = f"ws://{args.host}:{args.port}/ws?token={args.token}"
    print(f"[PC] Connecting to room hub at {url}")

    ws = websocket.create_connection(url, timeout=15)
    print("[PC] Socket open. Sending HELLO (requests pairing approval on the Mac)...")
    ws.send(_msg("hello", {
        "device_name": args.name,
        "platform": "windows",
        "os_version": "10/11",
        "agent_version": "ira-roomhub-1.0",
        "capabilities": ["chat", "command_relay", "status_read"],
        "permissions": ["send_command", "read_status"],
    }))

    paired = False
    try:
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            mtype = (msg.get("type") or "").lower()
            payload = msg.get("payload", {}) or {}

            if mtype == "pair_request":
                print("[PC] Pairing request received on the Mac. APPROVE it in IRA's "
                      "Brahma Connect panel (or run: POST /gateway/pending/<id>/approve).")
                print(f"      pending_id={payload.get('pending_id')}")

            elif mtype == "pair_approved":
                paired = True
                dev = payload.get("device", {})
                print(f"[PC] PAIRED ✅ as device {dev.get('device_id')} "
                      f"({dev.get('name')}). You can now type commands.")
                # prove the link is live
                ws.send(_msg("ping"))
                break

            elif mtype == "error":
                print(f"[PC] Gateway error: {payload.get('error')}")
                return 1

            else:
                print(f"[PC] <-- {mtype}: {json.dumps(payload)[:160]}")
    except websocket.WebSocketConnectionClosedException:
        print("[PC] Connection closed by Mac.")
        return 1

    if not paired:
        print("[PC] Did not receive pair_approved. Approve the request on the Mac and retry.")
        return 1

    print("[PC] Linked. Type a command for IRA (e.g. 'turn on the bedroom light'); "
          "Ctrl-C to quit.")
    try:
        while True:
            text = input("you> ").strip()
            if not text:
                continue
            ws.send(_msg("chat_message", {"text": text}))
    except (KeyboardInterrupt, EOFError):
        print("\n[PC] Bye.")
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
