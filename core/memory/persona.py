"""
memory/persona.py — Persona markdown file system for IRA (ported from Aurum).

Manages markdown files that shape IRA's behavior and are injected into the
system prompt:
  SOUL.md       — personality, tone, boundaries
  IDENTITY.md   — name, role, presentation
  USER.md       — Yuvan's profile, preferences, projects
  MEMORY.md     — persistent long-term facts (AI-editable via heartbeat)
  HEARTBEAT.md  — instructions for the heartbeat maintenance pass
  AGENTS.md     — agent operating rules
  TOOLS.md      — tool conventions
  persona_config.json — settings for background tasks
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("persona")

BASE_DIR = Path(__file__).resolve().parent.parent
PERSONA_DIR = BASE_DIR / "core" / "persona"
PERSONA_CONFIG_FILE = PERSONA_DIR / "persona_config.json"

DEFAULT_PERSONA_CONFIG: Dict[str, Any] = {
    "heartbeat_enabled": False,
    "heartbeat_interval_minutes": 30,
    "system_overlay": "",
}

# ── Default content ────────────────────────────────────────────────────────

DEFAULT_SOUL = """# SOUL

You are IRA. You are Yuvan's personal AI assistant — efficient, professional, direct, and warm.

## Core truths
- Be genuinely helpful, not performatively helpful.
- Be resourceful before asking. Use your tools and memory, then ask only if truly stuck.
- Earn trust through competence. Yuvan gave you access to their work — do not make them regret it.
- Be bold with internal actions. Be careful with external ones (sending, deleting, spending).

## Vibe
Concise when possible, thorough when it matters. Direct. Not a corporate drone, not a sycophant.
"""

DEFAULT_IDENTITY = """# IDENTITY

## Name
IRA (Intelligent Responsive Assistant)

## Role
Personal AI assistant — voice, system control, web, files, messaging, automation

## Owner
Yuvan

## Voice
Direct, warm, efficient
"""

DEFAULT_USER = """# USER

## Basics
- Name: Yuvan

## Preferences
- Add preferences here as they're discovered

## Current focus
- Add projects and priorities here
"""

DEFAULT_MEMORY = """# MEMORY

Durable facts about Yuvan and ongoing work. IRA updates this during heartbeat passes.

## About Yuvan
(learned over time)

## Ongoing projects
(learned over time)

## Standing preferences
(learned over time)
"""

DEFAULT_HEARTBEAT = """# HEARTBEAT

Periodic self-maintenance pass (runs in background).

## Your job
1. Read the RECENT_ACTIVITY provided.
2. Compare against CURRENT_MEMORY.md.
3. If activity contains durable facts not yet in MEMORY.md, output the FULL updated MEMORY.md.
4. If MEMORY.md is already accurate, reply with exactly: NO_CHANGE

## What belongs in MEMORY.md
- Names, preferences, deadlines, projects, decisions
- Things Yuvan explicitly asked you to remember

## What does NOT belong
- Venting, temporary moods, one-off complaints
- Never invent facts. Only record what actually appears in the activity.
"""

DEFAULT_AGENTS = """# AGENTS

## Operating rules
- Think step by step before complex tasks.
- Confirm before irreversible actions (deleting files, sending messages).
- Record durable facts to memory (names, deadlines, decisions, preferences).
- Do not memorize venting or complaints.
- Update existing memories rather than creating duplicates.

## Communication
- Match Yuvan's energy: short message → concise reply.
- Be proactive when you have useful information.
"""

DEFAULT_TOOLS = """# TOOLS

## Installed tools
All action modules are available as tools. Use them by name when they match Yuvan's request.

## Conventions
- Always use the right tool for the job — never simulate results.
- If a tool fails, report the error clearly.
"""

PERSONA_FILE_SEEDS = [
    ("SOUL.md", DEFAULT_SOUL),
    ("IDENTITY.md", DEFAULT_IDENTITY),
    ("USER.md", DEFAULT_USER),
    ("MEMORY.md", DEFAULT_MEMORY),
    ("HEARTBEAT.md", DEFAULT_HEARTBEAT),
    ("AGENTS.md", DEFAULT_AGENTS),
    ("TOOLS.md", DEFAULT_TOOLS),
]

# ── Layout ─────────────────────────────────────────────────────────────────

def ensure_persona_layout():
    """Create persona directory and seed missing files."""
    PERSONA_DIR.mkdir(parents=True, exist_ok=True)
    for fname, content in PERSONA_FILE_SEEDS:
        fp = PERSONA_DIR / fname
        if not fp.exists():
            fp.write_text(content, encoding="utf-8")
            log.info("seeded persona file: %s", fname)
    if not PERSONA_CONFIG_FILE.exists():
        PERSONA_CONFIG_FILE.write_text(
            json.dumps(DEFAULT_PERSONA_CONFIG, indent=2) + "\n",
            encoding="utf-8",
        )


def read(name: str) -> str:
    """Read a persona markdown file by name ('SOUL', 'memory', etc.)."""
    valid = {f.upper(): f for f in [p[0] for p in PERSONA_FILE_SEEDS]}
    name_upper = name.upper().strip()
    if not name_upper.endswith(".MD"):
        name_upper += ".MD"

    fname = valid.get(name_upper)
    if not fname:
        # Try direct match
        for f, _ in PERSONA_FILE_SEEDS:
            if f.upper() == name_upper:
                fname = f
                break
    if not fname:
        return ""

    path = PERSONA_DIR / fname
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write(name: str, content: str) -> dict:
    """Write content to a persona file. Returns {"ok": True} or {"error": ...}."""
    # Resolve name
    target = None
    for f, _ in PERSONA_FILE_SEEDS:
        if f.upper().startswith(name.upper().rstrip(".MD")):
            target = f
            break
    if not target:
        return {"error": f"Unknown persona file: {name}"}

    path = PERSONA_DIR / target
    content = (content or "")[:128000]
    try:
        ensure_persona_layout()
        path.write_text(content, encoding="utf-8")
        log.info("persona %s updated", target)
        return {"ok": True, "file": target}
    except OSError as e:
        return {"error": str(e)}


def system_block() -> str:
    """Build the persona block for system prompt injection."""
    ensure_persona_layout()
    parts = []
    for fname in ("SOUL.md", "IDENTITY.md", "MEMORY.md", "AGENTS.md", "TOOLS.md"):
        fp = PERSONA_DIR / fname
        if fp.exists():
            c = fp.read_text(encoding="utf-8").strip()
            if c:
                parts.append(c)
    if not parts:
        return ""
    return "\n\n=== PERSONA ===\n" + "\n\n".join(parts)


def load_config() -> dict:
    """Load persona config with defaults."""
    ensure_persona_layout()
    if not PERSONA_CONFIG_FILE.exists():
        return dict(DEFAULT_PERSONA_CONFIG)
    try:
        data = json.loads(PERSONA_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(DEFAULT_PERSONA_CONFIG)
        merged = dict(DEFAULT_PERSONA_CONFIG)
        merged.update(data)
        return merged
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_PERSONA_CONFIG)


def save_config(updates: dict) -> dict:
    """Update persona config with new values."""
    cur = load_config()
    cur.update(updates)
    if "heartbeat_interval_minutes" in cur:
        cur["heartbeat_interval_minutes"] = max(1, int(cur.get("heartbeat_interval_minutes", 30)))
    ensure_persona_layout()
    PERSONA_CONFIG_FILE.write_text(
        json.dumps(cur, indent=2) + "\n",
        encoding="utf-8",
    )
    return dict(cur)


def list_files() -> list:
    """List all persona files with metadata."""
    ensure_persona_layout()
    result = []
    for fname, _ in PERSONA_FILE_SEEDS:
        fp = PERSONA_DIR / fname
        result.append({
            "name": fname.replace(".md", "").lower(),
            "filename": fname,
            "exists": fp.exists(),
            "size": fp.stat().st_size if fp.exists() else 0,
        })
    return result
