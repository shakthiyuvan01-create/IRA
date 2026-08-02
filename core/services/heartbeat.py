"""
services/heartbeat.py — autonomous self-maintenance loop (ported from Aurum).

On a timer, IRA reads its recent conversation activity, compares it against
MEMORY.md (from the persona system), and rewrites memory to capture durable
new facts.

Gated behind a heartbeat_enabled flag (OFF by default).
Only ever touches MEMORY — never other persona files.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("services.heartbeat")

BASE_DIR = Path(__file__).resolve().parent.parent.parent

_heartbeat_lock = threading.Lock()

MAINTENANCE_SYSTEM_PROMPT = (
    "You are IRA's maintenance brain. Your job is to keep MEMORY.md accurate.\n\n"
    "Read RECENT_ACTIVITY below, compare against "
    "CURRENT_MEMORY_MD, then:\n"
    "- If the activity contains durable facts not yet in MEMORY.md, output the FULL "
    "updated MEMORY.md (keep existing structure and headers).\n"
    "- If MEMORY.md is already accurate, reply with exactly: NO_CHANGE\n\n"
    "Rules:\n"
    "- Only update MEMORY.md. Never suggest changes to other files.\n"
    "- Names, preferences, deadlines, projects, decisions go in memory.\n"
    "- Venting, complaints, one-off moods do NOT belong.\n"
    "- Never invent facts. Only record what actually appears in the activity.\n"
    "- Output valid markdown starting with # header if updating."
)


def _recent_activity() -> str:
    """Read recent chat history from the JSONL file."""
    chat_log = BASE_DIR / "core" / "memory" / "chat_history.jsonl"
    if not chat_log.exists():
        return ""

    lines = []
    try:
        with chat_log.open("r", encoding="utf-8") as f:
            all_lines = f.readlines()
        # Get last 40 lines
        for line in all_lines[-40:]:
            try:
                r = json.loads(line)
                ts = r.get("ts", "")
                role = r.get("role", "?")
                text = (r.get("text", "") or "")[:200]
                if text:
                    lines.append(f"[{ts}] {role}: {text}")
            except Exception:
                continue
    except Exception as e:
        log.debug("activity read: %s", e)

    return "\n".join(lines)[:6000]


def _read_memory_md() -> str:
    """Read the current MEMORY.md content."""
    mem_path = BASE_DIR / "core" / "persona" / "MEMORY.md"
    if mem_path.exists():
        return mem_path.read_text(encoding="utf-8")
    return "# MEMORY\n\nDurable facts about the user and ongoing work.\n"


def _write_memory_md(content: str) -> bool:
    """Write new MEMORY.md content."""
    if not content or not content.startswith("#"):
        log.info("heartbeat: output not applied (not markdown)")
        return False
    mem_path = BASE_DIR / "core" / "persona" / "MEMORY.md"
    try:
        mem_path.parent.mkdir(parents=True, exist_ok=True)
        mem_path.write_text(content[:12000], encoding="utf-8")
        return True
    except OSError as e:
        log.error("heartbeat write failed: %s", e)
        return False


def run_tick(force: bool = False) -> Dict[str, Any]:
    """Run one heartbeat maintenance pass.

    Args:
        force: Skip rate limiting and min-activity checks when True.

    Returns:
        {"ok": True, "updated": True/False} or {"error": ...}
    """
    if not _heartbeat_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "heartbeat already running"}

    try:
        from core.providers import AI

        activity = _recent_activity()
        if len(activity) < 40 and not force:
            return {"skipped": True, "reason": "not enough recent activity"}

        current_memory = _read_memory_md()

        user_msg = (
            "=== RECENT_ACTIVITY ===\n"
            f"{activity}\n\n"
            "=== CURRENT_MEMORY_MD ===\n"
            f"{current_memory}"
        )

        result = AI.generate(
            user_msg,
            system=MAINTENANCE_SYSTEM_PROMPT,
            model="gpt-4o-mini",
            max_tokens=1200,
            temperature=0.2,
        )

        if not result or result.startswith("[AI error"):
            return {"error": "generation failed"}

        result = result.strip()
        if result == "NO_CHANGE" or result[:20].strip() == "NO_CHANGE":
            log.info("heartbeat: memory already accurate")
            return {"ok": True, "updated": False}

        ok = _write_memory_md(result)
        if ok:
            log.info("heartbeat: MEMORY.md updated (%d chars)", len(result))
            return {"ok": True, "updated": True, "memory_chars": len(result)}
        else:
            return {"ok": True, "updated": False, "note": "output not applied"}

    except Exception as e:
        log.error("heartbeat run_tick failed: %s", e)
        return {"error": str(e)}
    finally:
        _heartbeat_lock.release()


_last_run: float = 0.0


def supervisor():
    """Called by background task manager; checks interval before running."""
    global _last_run
    config_path = BASE_DIR / "core" / "persona" / "persona_config.json"
    enabled = True
    interval_min = 30
    try:
        if config_path.exists():
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            enabled = cfg.get("heartbeat_enabled", True)
            interval_min = max(1, int(cfg.get("heartbeat_interval_minutes", 30)))
    except Exception:
        pass

    if not enabled:
        return

    now = time.time()
    if now - _last_run < interval_min * 60 and interval_min > 0:
        return
    _last_run = now
    run_tick()
