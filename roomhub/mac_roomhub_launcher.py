#!/usr/bin/env python3
"""
Mac-side launcher for the IRA 24/7 room hub gateway.

Run this ON THE MAC (not on Windows). It starts the brahma_connect gateway so
your Windows PC can pair to it over the LAN. Pairing is TOKEN-BASED and
auto-approves, so NO on-screen click is needed (works headless).

One-time setup on the Mac:
    cd IRA
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements_headless.txt

Configure (env vars, no file editing required):
    export ROOMHUB_HOST=192.168.x.x     # your Mac's LAN IP
    export ROOMHUB_TOKEN=some-long-random-string   # MANDATORY on LAN
    export ROOMHUB_PORT=8765            # optional

Then:
    source .venv/bin/activate
    python roomhub/mac_roomhub_launcher.py

Keep awake 24/7:
    caffeinate -ims python roomhub/mac_roomhub_launcher.py
(or the App Store app "Amphetamine", or a launchd plist.)

Find your MAC_LAN_IP with:  ipconfig getifaddr en0
Generate a token with:      python -c "import secrets;print(secrets.token_urlsafe(32))"
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

# ── EDIT THESE (or set the env vars above) ──────────────────────────────────
MAC_LAN_IP = os.getenv("ROOMHUB_HOST")
ADMIN_TOKEN = os.getenv("ROOMHUB_TOKEN")
PORT = int(os.getenv("ROOMHUB_PORT", "8765"))
# ────────────────────────────────────────────────────────────────────────────

if not MAC_LAN_IP:
    # Try to autodetect the LAN IP so the user doesn't HAVE to set it.
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        MAC_LAN_IP = s.getsockname()[0]
        s.close()
    except Exception:
        MAC_LAN_IP = "127.0.0.1"

if not ADMIN_TOKEN:
    # Generate a one-time token so the gateway will actually start on the LAN.
    # Persisted to the config file below so reboots keep the same token.
    ADMIN_TOKEN = secrets.token_urlsafe(32)
    print(f"[Mac] No ROOMHUB_TOKEN set — generated one and persisting it:\n      {ADMIN_TOKEN}")
    print("[Mac] Keep this token secret; the Windows PC needs it to pair.")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from brahma_connect.service import get_service  # noqa: E402

# Persist gateway config (host + token) so IRA picks it up on next boot too.
cfg_path = ROOT / "core" / "config" / "brahma_connect.json"
cfg_path.parent.mkdir(parents=True, exist_ok=True)
cfg_path.write_text(
    json.dumps({
        "host": MAC_LAN_IP,
        "port": PORT,
        "enabled": True,
        "advertise": True,
        "admin_token": ADMIN_TOKEN,
    }, indent=2),
    encoding="utf-8",
)

svc = get_service(ROOT)
svc.start_background()
print(f"[Mac] Brahma Connect gateway starting at http://{MAC_LAN_IP}:{PORT}")
print(f"[Mac] Pair from Windows with:")
print(f"      python roomhub/pc_pair_headless.py --host {MAC_LAN_IP} "
      f"--port {PORT} --token {ADMIN_TOKEN}")
print("[Mac] Gateway running. Press Ctrl-C to stop (or just close this "
      "terminal if launched via launchd/Amphetamine).")

try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    svc.stop()
    print("[Mac] Gateway stopped.")
