"""
core/user_paths.py — Resolve where IRA saves user-facing files (Desktop, etc.).

Priority for the Desktop folder:
  1. "desktop_path" in config/api_keys.json  (explicit override — highest priority)
  2. A OneDrive-redirected Desktop if one exists
  3. The classic ~/Desktop
"""
import json
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _cfg() -> dict:
    try:
        return json.loads((_base_dir() / "core" / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def desktop_dir() -> Path:
    # 1) Explicit override from config.
    p = str(_cfg().get("desktop_path", "") or "").strip()
    if p and "REPLACE_WITH" not in p:
        return Path(p)

    # 2) OneDrive-redirected Desktop, if present.
    home = Path.home()
    for cand in (home / "OneDrive" / "Desktop",
                 home / "OneDrive" / "Links" / "Desktop"):
        if cand.exists():
            return cand

    # 3) Classic Desktop.
    return home / "Desktop"
