"""
command_runner.py — IRA runs cmd / PowerShell commands for Yuvan.

Safety: commands that look destructive (delete, format, shutdown, registry
edits, etc.) are refused unless confirm=True is passed. IRA is instructed to
ask Yuvan for confirmation first, then re-run with confirm=true.
"""
import subprocess
import sys

# Substrings that flag a command as potentially destructive.
_DANGER = [
    "del ", "erase ", "rm ", "rm -", "rmdir", "rd /s", "remove-item", "ri ",
    "format ", "diskpart", "mkfs", "fdisk", "deltree", "cipher /w", "sdelete",
    "reg delete", "reg add", "shutdown", "restart-computer", "stop-computer",
    "takeown", "icacls", "net user", "bcdedit", "> ", "clear-content",
]


def _is_dangerous(cmd: str) -> bool:
    c = f" {cmd.lower()} "
    return any(tok in c for tok in _DANGER)


def run_command(parameters=None, response=None, player=None, session_memory=None) -> str:
    params  = parameters or {}
    cmd     = (params.get("command") or params.get("cmd") or "").strip()
    confirm = bool(params.get("confirm", False))

    if not cmd:
        return "What command should I run, Yuvan?"

    if _is_dangerous(cmd) and not confirm:
        return (
            f"That command looks destructive, Yuvan:\n  {cmd}\n"
            "I won't run it until you confirm. Say 'yes, run it' and I will."
        )

    try:
        if sys.platform == "win32":
            full = ["powershell", "-NoProfile", "-Command", cmd]
        else:
            full = ["bash", "-lc", cmd]
        proc = subprocess.run(full, capture_output=True, text=True, timeout=60)
        out  = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
        if len(out) > 1500:
            out = out[:1500] + "…"
        status = "success" if proc.returncode == 0 else f"exit code {proc.returncode}"
        return f"Command finished ({status}), Yuvan:\n{out}"
    except subprocess.TimeoutExpired:
        return "The command timed out after 60 seconds, Yuvan."
    except FileNotFoundError:
        return "I couldn't find the shell to run that, Yuvan."
    except Exception as e:
        return f"Command failed, Yuvan: {e}"
