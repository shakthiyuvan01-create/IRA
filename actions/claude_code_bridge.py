from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

# Default workspace for generated code. Prefer D: (per user convention: installs
# and generated projects live on D:, never C:). Falls back to IRA's own folder
# if D: is unavailable.
DEFAULT_WORKSPACE = Path("D:/IRA_Projects")
if not DEFAULT_WORKSPACE.parent.exists():
    DEFAULT_WORKSPACE = BASE_DIR / "Generated"


def _resolve_claude() -> str:
    """Locate the Claude Code CLI executable."""
    found = shutil.which("claude")
    if found:
        return found
    # Common WinGet install path
    cand = (Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" /
            "Packages" / "Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe" /
            "claude.exe")
    if cand.exists():
        return str(cand)
    raise FileNotFoundError("Claude Code CLI (claude) not found on PATH.")


def _selected_workspace(parameters: dict[str, Any]) -> Path:
    configured = str(parameters.get("workspace_path") or
                     parameters.get("project_dir") or
                     parameters.get("output_dir") or "").strip()
    ws = Path(configured) if configured else DEFAULT_WORKSPACE
    try:
        ws.mkdir(parents=True, exist_ok=True)
    except Exception:
        ws = BASE_DIR / "Generated"
        ws.mkdir(parents=True, exist_ok=True)
    return ws


def run_developer_mode_request(parameters: dict[str, Any], speak=None) -> str:
    """Send a code-generation request to Claude Code (CLI) and return its result.

    Every coding task in IRA now routes through Claude Code — there is no local
    code-generation fallback. Progress is spoken back to the user via `speak`.
    """
    params = dict(parameters or {})
    description = str(params.get("description") or "").strip()
    if not description:
        return "No coding task description was provided."

    workspace = _selected_workspace(params)
    claude = _resolve_claude()

    # Build a self-contained prompt. Claude Code reads CLAUDE.md from cwd, but we
    # keep the instruction explicit so behaviour is predictable.
    prompt = (
        f"You are IRA's coding engine, running inside Claude Code. "
        f"Create the requested project/files in the current working directory "
        f"({workspace}).\n\nTask:\n{description}\n\n"
        f"Requirements:\n"
        f"- Build complete, working, production-quality code.\n"
        f"- Do not ask for confirmation — implement the full task.\n"
        f"- When done, give a short summary of what was created and its path."
    )

    if speak:
        try:
            speak(f"Starting Claude Code to build: {description[:120]}")
        except Exception:
            pass

    cmd = [
        claude,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]

    result_parts: list[str] = []
    spoke_at = 0.0
    import time
    t0 = time.time()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as e:
        return f"Failed to launch Claude Code: {e}"

    assert proc.stdout is not None
    for raw in proc.stdout:
        raw = raw.strip()
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except Exception:
            continue
        etype = evt.get("type")
        if etype == "result":
            val = evt.get("result") or evt.get("content") or ""
            if isinstance(val, list):
                val = " ".join(
                    p.get("text", "") for p in val if isinstance(p, dict)
                )
            result_parts.append(val)
        elif etype == "assistant":
            msg = evt.get("message", {})
            if isinstance(msg, dict):
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        result_parts.append(block.get("text", ""))

        # Speak incremental progress roughly every 12s.
        if speak and (time.time() - spoke_at) > 12 and result_parts:
            last = result_parts[-1].strip().splitlines()
            snippet = last[-1] if last else ""
            if len(snippet) > 3:
                try:
                    speak(f"Claude Code: {snippet[:140]}")
                    spoke_at = time.time()
                except Exception:
                    pass

    proc.wait()
    final = "\n".join(p for p in result_parts if p).strip()
    if not final:
        final = f"Claude Code finished (exit {proc.returncode}). No text output captured."

    if speak:
        try:
            speak("Claude Code finished the build.")
        except Exception:
            pass

    return final
