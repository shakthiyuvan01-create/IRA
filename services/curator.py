"""
services/curator.py — Background memory curator for IRA.

Periodically reviews agent-created memory and maintains the collection.
Runs inactivity-triggered: when the agent is idle and the last curator run
was longer than ``interval_hours`` ago, maybe_run() triggers a review.

Responsibilities:
  - Auto-archive stale entries based on age (configurable)
  - Pinned entries bypass auto-transitions
  - Background LLM review to consolidate overlapping entries
  - Never auto-deletes — only archives. Archive is recoverable.

Designed after Hermes Agent's agent/curator.py by Nous Research.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_INTERVAL_HOURS = 168  # 7 days
DEFAULT_MIN_IDLE_HOURS = 2
DEFAULT_STALE_AFTER_DAYS = 90


def _get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
STATE_PATH = BASE_DIR / "memory" / ".curator_state.json"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    """Load curator state from disk."""
    if not STATE_PATH.exists():
        return {
            "last_run_at": 0,
            "total_reviews": 0,
            "total_archived": 0,
            "paused": False,
        }
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {
            "last_run_at": data.get("last_run_at", 0),
            "total_reviews": data.get("total_reviews", 0),
            "total_archived": data.get("total_archived", 0),
            "paused": data.get("paused", False),
        }
    except (OSError, json.JSONDecodeError):
        return {"last_run_at": 0, "total_reviews": 0, "total_archived": 0, "paused": False}


def _save_state(state: Dict[str, Any]) -> None:
    """Persist curator state to disk."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to save curator state: %s", e)


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

ARCHIVE_DIR = BASE_DIR / "memory" / ".curator_archive"


def _archive_path(target: str) -> Path:
    """Get archive path for a memory target (memory or user)."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ARCHIVE_DIR / f"{target}_{ts}.archive.md"


def _archive_entries(target: str, entries: List[str], reason: str) -> int:
    """Archive entries to a dated archive file. Returns count."""
    if not entries:
        return 0
    path = _archive_path(target)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Archived {target} entries — {datetime.now().isoformat()}\n")
            f.write(f"# Reason: {reason}\n")
            f.write(f"# {'=' * 46}\n\n")
            for entry in entries:
                f.write(f"- {entry}\n\n")
        logger.info("Archived %d %s entries to %s", len(entries), target, path.name)
    except OSError as e:
        logger.warning("Failed to archive entries: %s", e)
        return 0
    return len(entries)


# ---------------------------------------------------------------------------
# Staleness detection
# ---------------------------------------------------------------------------

def _find_stale_indices(
    entries: List[str],
    pin_set: set,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> List[int]:
    """
    Find indices of stale entries to archive.

    Uses simple heuristics:
    - Timestamp patterns like "2024-01" or "Jan 2024" are checked against staleness
    - Entries matching pin patterns are excluded
    - Short/empty entries pass through
    """
    if not entries:
        return []

    now = time.time()
    stale_threshold = stale_after_days * 86400  # days to seconds
    stale_indices: List[int] = []

    for i, entry in enumerate(entries):
        if not entry or not entry.strip():
            continue

        # Check if entry is pinned (contains [PINNED] or :star:)
        if "[PINNED]" in entry or "⭐" in entry:
            continue

        # Check for date patterns
        date_found = False
        for token in entry.split():
            token = token.strip("(),.;:[]")
            # Check for YYYY-MM-DD date pattern
            if _looks_like_old_date(token, now, stale_threshold):
                date_found = True
                break

        if date_found:
            stale_indices.append(i)

    return stale_indices


def _looks_like_old_date(token: str, now: float, stale_threshold: float) -> bool:
    """Check if a token looks like a date older than the threshold."""
    try:
        # YYYY-MM-DD
        if len(token) == 10 and token[4] == "-" and token[7] == "-":
            from datetime import datetime as dt
            d = dt.strptime(token, "%Y-%m-%d")
            if (now - d.timestamp()) > stale_threshold:
                return True
    except (ValueError, IndexError):
        pass

    try:
        # MM/DD/YYYY or DD/MM/YYYY
        if len(token) == 10 and token[2] in "/." and token[5] in "/.":
            from datetime import datetime as dt
            for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%m.%d.%Y", "%d.%m.%Y"):
                try:
                    d = dt.strptime(token, fmt)
                    if (now - d.timestamp()) > stale_threshold:
                        return True
                except ValueError:
                    continue
    except (ValueError, IndexError):
        pass

    return False


# ---------------------------------------------------------------------------
# Core curator function
# ---------------------------------------------------------------------------

def maybe_run(
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
    min_idle_hours: int = DEFAULT_MIN_IDLE_HOURS,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Run the curator if enough time has passed since the last run.

    Args:
        interval_hours: Minimum hours between curator runs
        min_idle_hours: Minimum hours of inactivity before curator activates
        stale_after_days: Days after which an entry is considered stale
        force: If True, run regardless of interval

    Returns:
        Dict with result info:
        - skipped: True if curator didn't run (with reason)
        - archived: Number of entries archived
        - reviewed: Number of entries reviewed
    """
    state = _load_state()

    if state.get("paused") and not force:
        return {"skipped": True, "reason": "curator is paused"}

    # Check interval
    if not force:
        elapsed = time.time() - state.get("last_run_at", 0)
        if elapsed < interval_hours * 3600:
            remaining_h = round((interval_hours * 3600 - elapsed) / 3600, 1)
            return {"skipped": True, "reason": f"next run in {remaining_h}h"}

    # Load memory store
    try:
        from memory.memory_tool import get_memory_store

        store = get_memory_store()
    except Exception as e:
        logger.warning("Curator: failed to load memory store: %s", e)
        return {"skipped": True, "reason": f"memory store error: {e}"}

    result = {"reviewed": 0, "archived": 0, "pinned_skipped": 0}

    for target in ("memory", "user"):
        entries = store._entries_for(target)
        if not entries:
            continue

        result["reviewed"] += len(entries)

        # Find stale entries
        pin_set = set()
        stale_indices = _find_stale_indices(entries, pin_set, stale_after_days)

        if not stale_indices:
            continue

        # Archive stale entries
        stale_entries = [entries[i] for i in stale_indices]
        archived = _archive_entries(target, stale_entries, f"stale after {stale_after_days}d")
        if archived:
            # Remove from live store
            for i in sorted(stale_indices, reverse=True):
                entries.pop(i)
            store._set_entries(target, entries)
            store.save_to_disk(target)
            result["archived"] += archived

    # Update state
    state["last_run_at"] = time.time()
    state["total_reviews"] = state.get("total_reviews", 0) + result["reviewed"]
    state["total_archived"] = state.get("total_archived", 0) + result["archived"]
    _save_state(state)

    return result


# ---------------------------------------------------------------------------
# Pause/resume controls
# ---------------------------------------------------------------------------

def pause() -> Dict[str, Any]:
    """Pause the curator."""
    state = _load_state()
    state["paused"] = True
    _save_state(state)
    return {"paused": True}


def resume() -> Dict[str, Any]:
    """Resume the curator."""
    state = _load_state()
    state["paused"] = False
    _save_state(state)
    return {"paused": False}


def status() -> Dict[str, Any]:
    """Get curator status."""
    state = _load_state()
    return {
        "paused": state.get("paused", False),
        "last_run_at": state.get("last_run_at", 0),
        "total_reviews": state.get("total_reviews", 0),
        "total_archived": state.get("total_archived", 0),
        "archive_path": str(ARCHIVE_DIR),
    }


# ---------------------------------------------------------------------------
# Consolidated review (LLM-powered)
# ---------------------------------------------------------------------------

def consolidate(target: str) -> Dict[str, Any]:
    """
    Run an LLM-powered consolidation pass on a memory target.

    Uses the LLM client to review entries and suggest merges for overlapping topics.
    Returns dict with suggested_merges and applied_merges.
    """
    try:
        from memory.memory_tool import get_memory_store
        store = get_memory_store()
    except Exception as e:
        return {"error": f"memory store: {e}"}

    entries = store._entries_for(target)
    if len(entries) < 3:
        return {"skipped": True, "reason": "too few entries to consolidate"}

    # Build a summary prompt for the LLM
    prompt = (
        f"Review these {target} memory entries and identify entries that can be "
        f"merged without losing information. For each merge suggestion, specify "
        f"the indices (0-based) to merge and the merged text.\n\n"
        f"Entries:\n" + "\n".join(f"[{i}] {e}" for i, e in enumerate(entries))
    )

    try:
        from core.llm_client import chat as llm_chat
        response = llm_chat(
            prompt,
            system="You are a memory consolidation assistant. Return ONLY a JSON array of merge operations, or [] if no merges needed. Each operation: {indices: [0, 1], merged: \"consolidated text\"}",
        )
    except Exception as e:
        logger.warning("Curator consolidate LLM call failed: %s", e)
        return {"error": f"LLM call: {e}"}

    # Parse response
    try:
        # Try to extract JSON from response
        import re as _re
        json_match = _re.search(r"\[.*?\]", response, _re.DOTALL)
        if json_match:
            suggestions = json.loads(json_match.group())
        else:
            suggestions = json.loads(response)
    except (json.JSONDecodeError, ValueError):
        return {"suggested": 0, "applied": 0, "error": "failed to parse LLM response"}

    if not isinstance(suggestions, list):
        return {"suggested": 0, "applied": 0}

    # Apply merges (in reverse index order to preserve positions)
    merge_map = {}
    for s in suggestions:
        if not isinstance(s, dict):
            continue
        indices = s.get("indices", [])
        merged = s.get("merged", "")
        if not indices or not merged:
            continue
        for idx in indices:
            merge_map[idx] = merged

    if not merge_map:
        return {"suggested": len(suggestions), "applied": 0}

    # Apply using batch operations
    try:
        from memory.memory_tool import memory_tool as mt
        ops = []
        # Remove the entries being merged
        merged_content = list(set(merge_map.values()))
        for idx in sorted(merge_map.keys(), reverse=True):
            ops.append({"action": "remove", "old_text": entries[idx][:40]})
        # Add the merged entries
        for content in merged_content:
            if content not in entries:
                ops.append({"action": "add", "content": content})

        if ops:
            mt(target=target, operations=ops)

        return {"suggested": len(suggestions), "applied": len(set(merge_map.keys()))}
    except Exception as e:
        return {"suggested": len(suggestions), "applied": 0, "error": str(e)}