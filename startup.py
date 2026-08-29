"""startup.py — make IRA launch automatically when Windows logs in.

Run ONCE (double-click, or:  .venv\Scripts\python.exe startup.py):
    - writes an HKCU "Run" entry pointing at Launch IRA.vbs (minimized, headless
      boot, no console window)
    - that entry fires every time THIS user logs in, so IRA is up 24/7

Disable later with:  python startup.py --disable
Status:              python startup.py --status

Why HKCU\...\Run and not the Startup folder / Task Scheduler:
    - HKCU Run runs at logon for the current user with zero elevation prompts.
    - Launch IRA.vbs starts the app minimized and hides the console window,
      so IRA boots quietly in the background like JARVIS should.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
VBS  = BASE / "Launch IRA.vbs"
KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "IRA"


def _vbs_target() -> str:
    if VBS.exists():
        return str(VBS)
    # Fallback: launch the python entry directly (minimized)
    return str(BASE / "run.bat")


def enable() -> None:
    import winreg
    target = _vbs_target()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, f'"{target}"')
    print(f"[startup] IRA will launch at next logon ({target}).")


def disable() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, NAME)
        print("[startup] IRA autostart removed.")
    except FileNotFoundError:
        print("[startup] IRA autostart was not set.")


def status() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            val, _ = winreg.QueryValueEx(k, NAME)
        print(f"[startup] ENABLED -> {val}")
    except FileNotFoundError:
        print("[startup] DISABLED (not in HKCU Run).")


def main() -> None:
    arg = (sys.argv[1].lower() if len(sys.argv) > 1 else "")
    if arg == "--disable":
        disable()
    elif arg == "--status":
        status()
    else:
        enable()
        # Also launch it right now so you don't have to wait for a reboot.
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", str(_vbs_target())],
                shell=False,
                creationflags=0x08000000,  # detached, no focus steal
            )
            print("[startup] IRA launching now (minimized).")
        except Exception as e:
            print(f"[startup] enable OK; could not auto-launch now: {e}")


if __name__ == "__main__":
    main()
