"""
services/experience_db.py — Experience learning system (ported from Aurum).

Every solved problem becomes reusable experience. After each team run or
complex task: problem -> solution -> lesson -> reusable strategy,
stored and retrievable. Injected into future tasks.
"""
import json
import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger("services.experience")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = BASE_DIR / "core" / "memory" / "experiences.json"


def _load() -> list:
    if DB_PATH.exists():
        try:
            data = json.loads(DB_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _save(entries: list):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_text(json.dumps(entries[-200:], indent=2, ensure_ascii=False), encoding="utf-8")


def learn(problem: str, solution: str, strategy: str = "", lesson: str = "") -> dict:
    """Store a reusable experience."""
    entries = _load()
    entries.append({
        "ts": time.time(),
        "problem": problem[:300],
        "solution": solution[:500],
        "strategy": strategy[:500],
        "lesson": lesson[:300],
        "uses": 0,
    })
    _save(entries)
    return {"ok": True, "count": len(entries)}


def recall(goal: str, limit: int = 3) -> str:
    """Find past experiences relevant to a goal."""
    words = [w.lower() for w in goal.split() if len(w) > 4][:6]
    if not words:
        return ""

    entries = _load()
    hits = []
    for e in entries:
        problem_lower = e.get("problem", "").lower()
        if any(w in problem_lower for w in words):
            hits.append(e)

    # Sort by use count descending, take top N
    hits.sort(key=lambda e: (e.get("uses", 0), e.get("ts", 0)), reverse=True)
    hits = hits[:limit]

    # Increment use counts
    for h in hits:
        h["uses"] = h.get("uses", 0) + 1
    _save(entries)

    if not hits:
        return ""

    return "\n".join(
        f"PAST EXPERIENCE: for '{e['problem'][:80]}' -> {e['strategy'][:200]}"
        for e in hits
    )


def list_experiences(limit: int = 30) -> list:
    return _load()[-limit:]
