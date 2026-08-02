"""
memory/memory_tool.py — Memory tool handler for IRA's Gemini integration.

Provides:
  - memory_tool(): single entry point for the memory tool (add/replace/remove/batch)
  - MEMORY_SCHEMA: Gemini-compatible function declaration
  - SESSION_SEARCH_SCHEMA: Gemini-compatible session search declaration
  - memory_store_instance: lazily initialized MemoryStore singleton

This module is imported by main.py to register tool declarations and dispatch
tool calls in _execute_tool().

Designed after Hermes Agent's tools/memory_tool.py by Nous Research.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.memory.memory_engine import MemoryStore

logger = logging.getLogger(__name__)

# Lazy singleton
_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """Get or create the MemoryStore singleton."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore.migrate_from_json()
    return _memory_store


def reload_memory_store() -> MemoryStore:
    """Force-reload the MemoryStore (e.g. after config change)."""
    global _memory_store
    _memory_store = MemoryStore()
    _memory_store.load_from_disk()
    return _memory_store


def reset_memory_store(store: MemoryStore) -> None:
    """Set a custom MemoryStore instance (for testing or pre-config)."""
    global _memory_store
    _memory_store = store


# ---------------------------------------------------------------------------
# Memory tool handler
# ---------------------------------------------------------------------------

def memory_tool(
    action: str = None,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    operations: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Two shapes:
      - Single op: action + (content / old_text).
      - Batch:     operations=[{action, content?, old_text?}, ...]

    Returns JSON string with results.
    """
    store = get_memory_store()

    if target is None:
        target = "memory"
    if target not in {"memory", "user"}:
        return json.dumps({
            "success": False,
            "error": f"Invalid target '{target}'. Use 'memory' or 'user'.",
        }, ensure_ascii=False)

    # --- Batch path ---------------------------------------------------------
    if operations:
        if not isinstance(operations, list):
            return json.dumps({
                "success": False,
                "error": "operations must be a list.",
            }, ensure_ascii=False)
        result = store.apply_batch(target, operations)
        return json.dumps(result, ensure_ascii=False)

    # --- Single-op path -----------------------------------------------------
    if action == "add":
        if not content:
            return json.dumps({
                "success": False,
                "error": "Content is required for 'add' action.",
            }, ensure_ascii=False)
        result = store.add(target, content)

    elif action == "replace":
        if not old_text or not content:
            missing = "old_text" if not old_text else "content"
            return json.dumps({
                "success": False,
                "error": f"{missing} is required for 'replace' action.",
            }, ensure_ascii=False)
        result = store.replace(target, old_text, content)

    elif action == "remove":
        if not old_text:
            return json.dumps({
                "success": False,
                "error": "old_text is required for 'remove' action. Provide a short unique substring of the entry to remove.",
            }, ensure_ascii=False)
        result = store.remove(target, old_text)

    else:
        return json.dumps({
            "success": False,
            "error": f"Unknown action '{action}'. Use: add, replace, remove, or use operations for batch.",
        }, ensure_ascii=False)

    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Session search handler
# ---------------------------------------------------------------------------

def session_search_tool(query: str, limit: int = 10) -> str:
    """
    Search across past conversation history using FTS5.
    Returns JSON with ranked results.
    """
    try:
        from core.memory.session_search import search as _search
        results = _search(query, limit=limit)
        if not results:
            return json.dumps({
                "success": True,
                "results": [],
                "message": f"No past conversations matched '{query}'.",
            }, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "results": results,
            "count": len(results),
        }, ensure_ascii=False)
    except ImportError:
        return json.dumps({
            "success": False,
            "error": "Session search is not available. The session_search module is not installed.",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Session search failed: {e}",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Gemini function-calling schemas
# ---------------------------------------------------------------------------

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. "
        "Memory is injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array "
        "(each item: {action, content?, old_text?}). The batch applies atomically and "
        "the char limit is checked only on the FINAL result — so a single call can "
        "remove/replace stale entries AND add new ones.\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). "
        "'memory' = your notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial info, easily re-discovered facts, raw data dumps, task progress, "
        "temporary state. Reusable procedures belong in a skill, not memory."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "The action to perform (single-op). Omit when using 'operations': add | replace | remove",
            },
            "target": {
                "type": "STRING",
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
            },
            "content": {
                "type": "STRING",
                "description": "The entry content. Required for 'add' and 'replace'.",
            },
            "old_text": {
                "type": "STRING",
                "description": "REQUIRED for 'replace' and 'remove': a short unique substring identifying the existing entry to modify.",
            },
            "operations": {
                "type": "ARRAY",
                "description": "Batch shape: list of operations applied atomically. Preferred over single-op. Each: {action, content?, old_text?}.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "action": {
                            "type": "STRING",
                            "description": "add | replace | remove",
                        },
                        "content": {
                            "type": "STRING",
                            "description": "Entry content for add/replace.",
                        },
                        "old_text": {
                            "type": "STRING",
                            "description": "Substring identifying the entry for replace/remove.",
                        },
                    },
                },
            },
        },
        "required": ["target"],
    },
}

SESSION_SEARCH_SCHEMA = {
    "name": "session_search",
    "description": (
        "Search across ALL past conversations with the user. Use this when the user "
        "asks about something discussed in a previous chat — a fact they mentioned, a "
        "decision made, a project discussed. Returns ranked results with timestamps "
        "and conversation excerpts. Call this silently and use the results naturally; "
        "do not announce that you are searching."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Search query — what to look for in past conversations.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum number of results (default 10).",
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Memory block formatter for system prompt
# ---------------------------------------------------------------------------

def get_system_prompt_blocks() -> List[str]:
    """
    Return formatted memory blocks for system prompt injection.
    Returns [user_block?, memory_block?] with both non-empty blocks.
    """
    store = get_memory_store()
    blocks = []
    for target in ("user", "memory"):
        block = store.format_for_system_prompt(target)
        if block:
            blocks.append(block)
    return blocks


def format_memory_for_prompt_legacy() -> str:
    """
    Legacy adapter — returns the same format as the old format_memory_for_prompt()
    for backward compatibility during migration.
    """
    store = get_memory_store()
    blocks = []
    for target in ("user", "memory"):
        block = store.format_for_system_prompt(target)
        if block:
            blocks.append(block)
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"