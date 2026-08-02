"""
memory/session_search.py — FTS5 full-text search across conversation history.

Uses SQLite FTS5 (built-in) to index chat_history.jsonl for fast cross-session recall.
Index is rebuilt on startup and kept in sync with new writes.

Designed after Hermes Agent's hermes_state.py FTS5 approach by Nous Research.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# Cap on user-controlled FTS5 query input
MAX_FTS5_QUERY_CHARS = 2_048


def _get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()

# Chat history path (same as memory/memory_manager.py uses)
_CHAT_HISTORY_DIR_CANDIDATE = Path("D:/IRA_Memory")
CHAT_HISTORY_DIR = _CHAT_HISTORY_DIR_CANDIDATE if _CHAT_HISTORY_DIR_CANDIDATE.exists() else BASE_DIR / "core" / "memory"
CHAT_LOG_PATH = CHAT_HISTORY_DIR / "chat_history.jsonl"

# SQLite DB for FTS index
DB_PATH = BASE_DIR / "core" / "data" / "session_search.db"


def _get_db() -> sqlite3.Connection:
    """Get or create the SQLite database with FTS5 support."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_fts_table(conn: sqlite3.Connection) -> None:
    """Create the FTS5 virtual table if it doesn't exist."""
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            row_id,
            timestamp,
            role,
            text,
            content='',
            tokenize='porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS fts_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()


def rebuild_index() -> int:
    """
    Rebuild the FTS index from the chat history JSONL file.
    Returns the number of messages indexed.
    """
    if not CHAT_LOG_PATH.exists():
        return 0

    with _lock:
        conn = _get_db()
        _ensure_fts_table(conn)
        conn.execute("DELETE FROM messages_fts")
        conn.execute("DELETE FROM fts_meta")

        count = 0
        try:
            with open(CHAT_LOG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = record.get("ts", "")
                    role = record.get("role", "unknown")
                    text = record.get("text", "")
                    if not text:
                        continue

                    row_id = f"{ts}_{role}_{count}"
                    # Escape single quotes for FTS5
                    text_clean = text.replace("'", "''")
                    ts_clean = ts.replace("'", "''")
                    role_clean = role.replace("'", "''")

                    try:
                        conn.execute(
                            "INSERT INTO messages_fts (row_id, timestamp, role, text) "
                            "VALUES (?, ?, ?, ?)",
                            (row_id, ts, role, text),
                        )
                        count += 1
                    except sqlite3.OperationalError:
                        continue

            conn.commit()
        except OSError as e:
            logger.warning("Failed to read chat history for FTS index: %s", e)
            conn.rollback()

        # Store metadata
        conn.execute(
            "INSERT OR REPLACE INTO fts_meta (key, value) VALUES (?, ?)",
            ("indexed_count", str(count)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO fts_meta (key, value) VALUES (?, ?)",
            ("indexed_at", str(time.time())),
        )
        conn.commit()
        conn.close()

    return count


def index_message(ts: str, role: str, text: str) -> None:
    """Index a single new message (called after each turn)."""
    if not text or not text.strip():
        return

    with _lock:
        try:
            conn = _get_db()
            _ensure_fts_table(conn)
            row_id = f"{ts}_{role}_{int(time.time() * 1000)}"
            conn.execute(
                "INSERT INTO messages_fts (row_id, timestamp, role, text) VALUES (?, ?, ?, ?)",
                (row_id, ts, role, text),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug("Failed to index message: %s", e)


def search(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Search across all indexed conversation history using FTS5.

    Args:
        query: Search text (will be sanitized for FTS5)
        limit: Maximum results to return

    Returns:
        List of dicts with keys: timestamp, role, text, rank
        Empty list if no results or search unavailable.
    """
    if not query or not query.strip():
        return []

    # Sanitize query for FTS5
    query = query.strip()[:MAX_FTS5_QUERY_CHARS]
    # Escape FTS5 special characters and build a prefix query
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []

    with _lock:
        try:
            conn = _get_db()
            _ensure_fts_table(conn)
            cursor = conn.execute(
                """
                SELECT timestamp, role, text, rank
                FROM messages_fts
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            )
            results = [
                {
                    "timestamp": row["timestamp"],
                    "role": row["role"],
                    "text": row["text"],
                    "rank": row["rank"],
                }
                for row in cursor.fetchall()
            ]
            conn.close()
            return results
        except sqlite3.OperationalError as e:
            # FTS5 may not be available or query syntax error
            logger.debug("FTS5 search failed: %s", e)
            return []
        except Exception as e:
            logger.debug("Session search error: %s", e)
            return []


def _sanitize_fts_query(query: str) -> str:
    """
    Sanitize a user query for FTS5 MATCH syntax.

    Removes FTS5 special characters and wraps terms appropriately.
    """
    # Remove FTS5 special characters
    special_chars = r'*""()^+-'
    for c in special_chars:
        query = query.replace(c, " ")

    # Split into words, filter empty, join with AND
    words = [w.strip() for w in query.split() if w.strip()]
    if not words:
        return ""

    # Use prefix matching for each word (append *)
    return " AND ".join(f'"{w}"*' for w in words)


def get_index_stats() -> Dict[str, Any]:
    """Get statistics about the session search index."""
    with _lock:
        try:
            conn = _get_db()
            _ensure_fts_table(conn)
            cursor = conn.execute("SELECT count(*) as cnt FROM messages_fts")
            indexed = cursor.fetchone()["cnt"]

            cursor = conn.execute(
                "SELECT value FROM fts_meta WHERE key = 'indexed_at'"
            )
            row = cursor.fetchone()
            indexed_at = float(row["value"]) if row else 0

            conn.close()
            return {
                "indexed_messages": indexed,
                "indexed_at": indexed_at,
                "chat_log_exists": CHAT_LOG_PATH.exists(),
            }
        except Exception:
            return {"indexed_messages": 0, "indexed_at": 0, "chat_log_exists": False}


def init_search() -> int:
    """
    Initialize the session search system.
    Rebuilds the index from chat history if needed.
    Returns the number of messages indexed.
    """
    stats = get_index_stats()
    if stats["indexed_messages"] == 0 and stats["chat_log_exists"]:
        count = rebuild_index()
        logger.info("Session search index rebuilt: %d messages", count)
        return count
    return stats["indexed_messages"]