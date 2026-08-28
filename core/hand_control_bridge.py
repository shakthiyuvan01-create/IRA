"""IRA Hand Control — bridge to the barehands air-board.

This module does NOT bundle or reimplement barehands. It *invokes* the
barehands server (expected at D:/barehands, run by the user) and adapts its
config to IRA's brand + per-user model. barehands itself is licensed
CC BY-NC-SA 4.0 (noncommercial); IRA keeps a clean boundary by treating it as
an external service over localhost.

Public API:
    HandControlBridge.ensure_user_folder(user)  -> Path   (creates per-user dirs)
    HandControlBridge.write_config(user)        -> bool   (writes barehands.json)
    HandControlBridge.set_state(state)          -> None   (drives the IRA "ring")
    HandControlBridge.server_url(role="stage")  -> str
    HandControlBridge.is_server_up()            -> bool
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

# barehands repo location (user-installed on D:). Overridable via env.
import os

_BAREHANDS_DIR = Path(
    os.environ.get("IRA_BAREHANDS_DIR", r"D:/barehands")
).resolve()
_SERVER_PORT = 8794
_STATE = ("idle", "listening", "thinking", "speaking")


class HandControlBridge:
    """Thin adapter between IRA and the external barehands server."""

    def __init__(self, barehands_dir: Path | None = None) -> None:
        self.barehands_dir = (barehands_dir or _BAREHANDS_DIR).resolve()

    # ── paths ──────────────────────────────────────────────────────────
    def user_folder(self, user: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (user or "default"))
        return Path(__file__).resolve().parent.parent / "Data" / "hand_control" / safe

    def ensure_user_folder(self, user: str) -> Path:
        base = self.user_folder(user)
        (base / "notes").mkdir(parents=True, exist_ok=True)
        (base / "media" / "misc").mkdir(parents=True, exist_ok=True)
        (base / "media" / "fx").mkdir(parents=True, exist_ok=True)
        (base / "media" / "models").mkdir(parents=True, exist_ok=True)
        (base / "media" / "holo").mkdir(parents=True, exist_ok=True)
        readme = base / "notes" / "Welcome.md"
        if not readme.exists():
            readme.write_text(
                "# IRA Hand Control — Your Notes\n\n"
                "These notes float on your air-board. Pinch to open, drag to move, "
                "clap to clear. Drop more `.md` files in this folder.\n",
                encoding="utf-8",
            )
        return base

    # ── config override (brand = IRA, orbs = this user's folder) ───────
    def write_config(self, user: str) -> bool:
        if not self.barehands_dir.exists():
            return False
        base = self.ensure_user_folder(user)
        cfg = {
            "name": "IRA",
            "port": _SERVER_PORT,
            "orbs": [
                {"title": "IRA Notes", "path": str(base / "notes"), "kind": "notes"},
                {"title": "IRA Props", "path": str(base / "media"), "kind": "media"},
            ],
        }
        try:
            (self.barehands_dir / "barehands.json").write_text(
                json.dumps(cfg, indent=2), encoding="utf-8"
            )
            return True
        except OSError:
            return False

    # ── ring state (IRA's face on the board) ──────────────────────────
    def set_state(self, state: str) -> None:
        state = state if state in _STATE else "idle"
        try:
            (self.barehands_dir / "state").mkdir(parents=True, exist_ok=True)
            (self.barehands_dir / "state" / "state").write_text(state, encoding="utf-8")
        except OSError:
            pass

    # ── server lifecycle ───────────────────────────────────────────────
    def server_url(self, role: str = "stage") -> str:
        if role == "render":
            return f"http://127.0.0.1:{_SERVER_PORT}/stage.html?role=render"
        return f"http://127.0.0.1:{_SERVER_PORT}/stage.html"

    def is_server_up(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", _SERVER_PORT), timeout=1.0):
                return True
        except OSError:
            return False

    def start_server(self) -> bool:
        """Launch the barehands server in the background if not already up."""
        if self.is_server_up():
            return True
        if not (self.barehands_dir / "server.py").exists():
            return False
        # Use the system python that has barehands' stdlib-only deps (none needed).
        py = sys.executable
        try:
            subprocess.Popen(
                [py, str(self.barehands_dir / "server.py")],
                creationflags=0x00000008,  # DETACHED_PROCESS on Windows
                close_fds=True,
            )
            return True
        except OSError:
            return False
