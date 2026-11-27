"""agent_reach.py — bridge from IRA to the Agent Reach CLI.

Agent Reach (https://github.com/Panniantong/Agent-Reach) gives an agent
read-only access to platforms that plain web search handles poorly:
YouTube / Bilibili subtitles, GitHub repos and issues, Reddit, X/Twitter,
V2EX, LinkedIn jobs, XiaoHongShu, Xueqiu quotes and more.

It is a separate command-line program, installed with uv:

    uv tool install https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip
    agent-reach setup

(The PyPI package called ``agent-reach`` is a different project — do not
``pip install agent-reach``.)

Every ``agent-reach`` command (1.6+) prints one JSON object:
``{"ok": true, "items": [...]}`` or ``{"ok": false, "error": {...}}``.
This module runs the CLI, parses that JSON and turns it into short text
IRA can speak or show.

Only read-only commands are exposed. Installing, logging in, adding
channels, configuring keys and uninstalling stay with the user in their
own terminal, so IRA never touches their accounts or credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

INSTALL_HINT = (
    "Agent Reach isn't installed yet, Yuvan. Install it once in a terminal:\n"
    "  uv tool install https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip\n"
    "  agent-reach setup\n"
    "Then ask me again."
)

DEFAULT_TIMEOUT = 150  # seconds; browser-backed channels can be slow
MAX_ITEMS = 8
MAX_TEXT_CHARS = 700
MAX_OUTPUT_CHARS = 6000

# Management commands that change installs, logins or keys. IRA refuses
# these and tells the user which command to run themselves.
_USER_ONLY = {
    "setup", "add", "remove", "login", "logout", "configure",
    "update", "uninstall", "skill",
}


# ── Locating and running the CLI ────────────────────────────────────────────

def find_agent_reach() -> str | None:
    """Return the path to the agent-reach executable, or None."""
    override = os.environ.get("AGENT_REACH_BIN", "").strip()
    if override and Path(override).exists():
        return override

    found = shutil.which("agent-reach")
    if found:
        return found

    # uv installs tools here, which may not be on PATH for a GUI launch
    # (e.g. IRA started from the .vbs shortcut on Windows).
    home = Path.home()
    exe = "agent-reach.exe" if sys.platform.startswith("win") else "agent-reach"
    candidates = [home / ".local" / "bin" / exe]
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "uv" / "tools" / "agent-reach" / "Scripts" / exe)
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def run_agent_reach(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run ``agent-reach <args>`` and return its JSON result.

    Never raises: failures come back as ``{"ok": False, "error": {...}}``
    using the same shape the CLI itself uses.
    """
    binary = find_agent_reach()
    if not binary:
        return {"ok": False, "error": {"code": "NOT_INSTALLED", "hint": INSTALL_HINT}}

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    kwargs: dict[str, Any] = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "TIMEOUT", "message": f"agent-reach took longer than {timeout}s"}}
    except OSError as e:
        return {"ok": False, "error": {"code": "LAUNCH_FAILED", "message": str(e)}}

    out = (proc.stdout or "").strip()
    parsed = _parse_json(out)
    if parsed is not None:
        return parsed

    # Plain-text output (``--text`` commands, or an old 1.5 install).
    if proc.returncode == 0:
        return {"ok": True, "text": out}
    message = (proc.stderr or out or f"exit code {proc.returncode}").strip()
    return {"ok": False, "error": {"code": "CLI_ERROR", "message": message[:500]}}


def _parse_json(out: str) -> dict[str, Any] | None:
    if not out:
        return None
    try:
        data = json.loads(out)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    # Tolerate stray log lines before the JSON object.
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
    return None


# ── Formatting ──────────────────────────────────────────────────────────────

_ERROR_ADVICE = {
    "NEED_SETUP": "That channel isn't set up yet. Run `agent-reach add {channel}` in a terminal.",
    "NEED_LOGIN": "That site needs a login. Run `agent-reach login {channel}` and sign in yourself.",
    "RATE_LIMITED": "The site is rate limiting us. Try again in a little while.",
    "PLATFORM_BLOCKED": "The site wants a captcha or verification. Open it in your browser and clear it first.",
    "BUSY": "Agent Reach's browser is busy right now. Try again in a minute.",
    "NOT_FOUND": "I couldn't find that content. Check the link or ID.",
    "BAD_INPUT": "Those parameters didn't work. Ask me to list Agent Reach channels to see the options.",
    "UPSTREAM_BROKEN": "That platform is having trouble right now.",
    "TIMEOUT": "Agent Reach timed out.",
}


def format_result(data: dict[str, Any], channel: str = "") -> str:
    """Turn an agent-reach JSON result into readable text."""
    if not data.get("ok"):
        err = data.get("error") or {}
        code = str(err.get("code") or "ERROR")
        if code == "NOT_INSTALLED":
            return INSTALL_HINT
        parts = [f"Agent Reach couldn't do that ({code})."]
        advice = _ERROR_ADVICE.get(code)
        if advice:
            parts.append(advice.format(channel=channel or "<channel>"))
        hint = err.get("hint")
        if hint and hint not in parts:
            parts.append(str(hint))
        elif err.get("message") and code not in ("NEED_LOGIN",):
            parts.append(str(err["message"])[:300])
        return " ".join(parts)

    channels = data.get("channels")
    if isinstance(channels, list) and channels:
        lines = ["Agent Reach channels:"]
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            actions = ", ".join(
                str(a.get("name")) for a in ch.get("actions") or [] if isinstance(a, dict)
            )
            name = ch.get("name") or "?"
            lines.append(f"- {name}: {actions}" if actions else f"- {name}")
        return _clip("\n".join(lines), MAX_OUTPUT_CHARS)

    if data.get("text"):
        return _clip(str(data["text"]), MAX_OUTPUT_CHARS)

    items = data.get("items")
    if items is None and isinstance(data.get("result"), dict):
        items = data["result"].get("items")

    if isinstance(items, list) and items:
        lines: list[str] = []
        for i, item in enumerate(items[:MAX_ITEMS], 1):
            if not isinstance(item, dict):
                lines.append(f"{i}. {_clip(str(item), MAX_TEXT_CHARS)}")
                continue
            title = str(item.get("title") or "").strip() or "(untitled)"
            meta = " · ".join(
                str(v) for v in (item.get("author"), item.get("published_at")) if v
            )
            lines.append(f"{i}. {title}" + (f" — {meta}" if meta else ""))
            if item.get("url"):
                lines.append(f"   {item['url']}")
            text = str(item.get("text") or "").strip()
            if text:
                lines.append("   " + _clip(" ".join(text.split()), MAX_TEXT_CHARS))
        more = len(items) - MAX_ITEMS
        if more > 0:
            lines.append(f"…and {more} more.")
        return _clip("\n".join(lines), MAX_OUTPUT_CHARS)

    if isinstance(items, list):
        return "Agent Reach found nothing for that."

    result = data.get("result")
    if result is not None:
        if isinstance(result, str):
            return _clip(result, MAX_OUTPUT_CHARS)
        return _clip(json.dumps(result, ensure_ascii=False, indent=1), MAX_OUTPUT_CHARS)
    return "Done."


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── IRA action entry point ──────────────────────────────────────────────────

def build_args(params: dict[str, Any]) -> tuple[list[str] | None, str]:
    """Map IRA tool parameters to agent-reach CLI args.

    Returns (args, error). ``args`` is None when the request is refused.
    """
    action = str(params.get("action") or "").strip().lower()
    url = str(params.get("url") or "").strip()
    channel = str(params.get("channel") or "").strip().lower()
    query = str(params.get("query") or "").strip()
    limit = params.get("limit")

    if not action:
        action = "read" if url else ("search" if query else "channels")

    if action in _USER_ONLY or channel in _USER_ONLY:
        return None, (
            f"I keep Agent Reach '{action}' for you to run yourself, Yuvan, since it changes "
            f"installs, logins or keys. Run `agent-reach {action}"
            f"{' ' + channel if channel and channel != action else ''}` in a terminal."
        )

    if action == "read":
        if not url:
            return None, "Give me a link to read with Agent Reach."
        return ["read", url], ""

    if action == "channels":
        return ["channels"], ""

    if action in ("status", "doctor"):
        return ["doctor", "--text"], ""

    if action == "check":
        if not channel:
            return None, "Tell me which Agent Reach channel to check."
        return ["check", channel, "--text"], ""

    if action == "version":
        return ["version"], ""

    # Channel action, e.g. youtube search / github read / xueqiu quote
    if not channel:
        return None, "Tell me which platform to use, like youtube, github, reddit or twitter."
    args = [channel, action]
    target = query or url
    if target:
        args.append(target)
    if limit:
        try:
            args += ["--limit", str(max(1, min(int(limit), 20)))]
        except (TypeError, ValueError):
            pass
    extra = params.get("args")
    if isinstance(extra, list):
        args += [str(a) for a in extra if str(a).strip()]
    return args, ""


def agent_reach(parameters=None, response=None, player=None, session_memory=None, speak=None) -> str:
    """IRA tool: read or search the web through Agent Reach."""
    params = dict(parameters or {})
    args, err = build_args(params)
    if args is None:
        return err

    channel = str(params.get("channel") or "").strip().lower()
    if player is not None:
        try:
            player.write_log(f"[Agent Reach] {' '.join(args)[:120]}")
        except Exception:
            pass

    data = run_agent_reach(args)
    return format_result(data, channel=channel)
