#!/usr/bin/env python3
"""
Headless PC->Mac pairing for the IRA room hub (NO GUI approval needed).

The Mac gateway (roomhub/mac_roomhub_launcher.py) exposes a token-based pairing
flow that auto-approves — perfect for a headless Mac with no clickable UI.

Flow:
  1. GET  http://HOST:PORT/gateway/pair   (needs admin token) -> pairing_token
  2. WS   ws://HOST:PORT/ws
  3. send pair_request {pairing_token, ...}  -> pair_approved {device, device_secret}
  4. send authenticate {device_id, device_secret} -> device_online  (LINK LIVE)

Usage (on the Windows PC, inside the IRA venv):
  python roomhub/pc_pair_headless.py --host 192.168.x.x --port 8765 --token ADMIN_TOKEN
  python roomhub/pc_pair_headless.py --host 192.168.x.x --port 8765 --token ADMIN_TOKEN --once
      (--once: pair + auth + ping, print success, and exit — for automated checks)

Deps: websocket-client  (pip install websocket-client)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import uuid


def _msg(msg_type: str, payload: dict | None = None) -> str:
    return json.dumps({
        "type": msg_type,
        "request_id": uuid.uuid4().hex,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload or {},
    })


def _rest_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    try:
        import websocket  # websocket-client
    except ImportError:
        sys.exit("Missing dep on the PC: pip install websocket-client")

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="Mac LAN IP")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", required=True, help="admin_token from the Mac gateway")
    ap.add_argument("--name", default="Windows-PC")
    ap.add_argument("--once", action="store_true",
                    help="pair + auth + ping then exit (no interactive prompt)")
    args = ap.parse_args()

    base = f"http://{args.host}:{args.port}"

    # 1) Fetch a pairing offer (this is the auto-approve token)
    print(f"[PC] Requesting pairing offer from {base}/gateway/pair ...")
    try:
        offer = _rest_get(f"{base}/gateway/pair", args.token)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        print(f"[PC] Pairing offer HTTP {e.code}: {body}")
        return 1
    except Exception as e:
        print(f"[PC] Cannot reach the Mac gateway: {e}")
        return 1

    pairing_token = (offer or {}).get("pairing_token")
    if not pairing_token:
        print(f"[PC] No pairing_token in offer response: {offer}")
        return 1
    print(f"[PC] Offer received (code={(offer or {}).get('pairing_code')}). Connecting WS...")

    # 2) WS connect + 3) pair_request (auto-approve) + 4) authenticate
    url = f"ws://{args.host}:{args.port}/ws"
    try:
        ws = websocket.create_connection(url, timeout=15)
    except Exception as e:
        print(f"[PC] WS connect failed: {e}")
        return 1

    print("[PC] WS open. Sending pair_request (token auto-approves)...")
    ws.send(_msg("pair_request", {
        "pairing_token": pairing_token,
        "device_name": args.name,
        "platform": "windows",
        "os_version": "10/11",
        "agent_version": "ira-roomhub-1.0",
        "capabilities": ["chat", "command_relay", "status_read"],
        "permissions": ["send_command", "read_status"],
    }))

    device_id = None
    while True:
        try:
            raw = ws.recv()
        except websocket.WebSocketConnectionClosedException:
            print("[PC] Connection closed by Mac before pairing completed.")
            return 1
        msg = json.loads(raw)
        mtype = (msg.get("type") or "").lower()
        payload = msg.get("payload", {}) or {}

        if mtype == "pair_approved":
            dev = payload.get("device", {})
            device_id = dev.get("device_id")
            secret = payload.get("device_secret")
            print(f"[PC] PAIRED as {device_id} ({dev.get('name')}). Authenticating...")
            ws.send(_msg("authenticate", {"device_id": device_id, "device_secret": secret}))
        elif mtype == "device_online":
            print("[PC] AUTHENTICATED. Room hub link is LIVE.")
            ws.send(_msg("ping"))
            break
        elif mtype == "error":
            print(f"[PC] Gateway error: {payload.get('error')}")
            return 1
        else:
            print(f"[PC] <-- {mtype}: {json.dumps(payload)[:160]}")

    if args.once:
        print("[PC] --once: link verified, exiting.")
        try:
            ws.close()
        except Exception:
            pass
        return 0

    print("[PC] Linked to Mac room hub. Type a command (Ctrl-C to quit):")
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
