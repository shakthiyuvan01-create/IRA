"""database.py — Shared SQLite database for IRA's content pipeline.

Uses Python's built-in sqlite3 module. Provides tables for:
- tasks: To-do items with due dates, priority, status
- articles: RSS/Web scraped content with rankings
- expenses: Financial transactions extracted from email
- mail_messages: Synced Gmail messages
- settings: Key-value settings store

Usage:
    from core.data.database import get_db, init_db
    db = get_db()
    db.add_task(title="Buy groceries", priority=3)
    tasks = db.list_tasks()
"""

import sqlite3
import sys
import json
from pathlib import Path
from datetime import datetime


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


BASE_DIR = _get_base_dir()
DATA_DIR = BASE_DIR / "core" / "data"
DB_PATH = DATA_DIR / "ira_content.db"


class Database:
    """SQLite database wrapper for IRA's content pipeline."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or DB_PATH)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=DELETE")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Schema ───────────────────────────────────────────────────────────────

    def init_schema(self):
        conn = self.connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                notes       TEXT,
                due_date    TEXT,
                priority    INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'open',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS articles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT,
                url             TEXT UNIQUE,
                summary         TEXT,
                raw_markdown    TEXT,
                source          TEXT,
                topic_tags      TEXT,
                published_at    TEXT,
                scraped_at      TEXT NOT NULL DEFAULT (datetime('now')),
                score           INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'new'
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                type            TEXT NOT NULL CHECK(type IN ('income', 'expense')),
                amount          REAL NOT NULL,
                currency        TEXT DEFAULT 'INR',
                category        TEXT DEFAULT 'Other',
                merchant        TEXT,
                description     TEXT,
                occurred_at     TEXT,
                source          TEXT DEFAULT 'manual',
                message_id      TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS mail_messages (
                id              TEXT PRIMARY KEY,
                thread_id       TEXT,
                from_addr       TEXT,
                from_name       TEXT,
                subject         TEXT,
                snippet         TEXT,
                body            TEXT,
                internal_date   INTEGER,
                labels          TEXT,
                processed       INTEGER DEFAULT 0,
                synced_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS draft_versions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id        INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                caption         TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.commit()

    # ── Tasks ────────────────────────────────────────────────────────────────

    def add_task(self, title: str, notes: str | None = None,
                 due_date: str | None = None, priority: int = 0) -> dict:
        conn = self.connect()
        cur = conn.execute(
            "INSERT INTO tasks (title, notes, due_date, priority) VALUES (?, ?, ?, ?)",
            (title, notes, due_date, priority)
        )
        conn.commit()
        return self.get_task(cur.lastrowid)

    def get_task(self, task_id: int) -> dict | None:
        conn = self.connect()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, due_date: str | None = None,
                   status: str | None = None) -> list[dict]:
        conn = self.connect()
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if due_date:
            query += " AND due_date = ?"
            params.append(due_date)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY priority DESC, due_date ASC, created_at DESC"
        return [dict(r) for r in conn.execute(query, params).fetchall()]

    def update_task(self, task_id: int, **kwargs) -> dict | None:
        allowed = {"title", "notes", "due_date", "priority", "status"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return self.get_task(task_id)
        updates["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]
        conn = self.connect()
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        conn.commit()
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> bool:
        conn = self.connect()
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0

    def complete_task(self, task_id: int) -> dict | None:
        return self.update_task(task_id, status="done")

    # ── Articles ─────────────────────────────────────────────────────────────

    def add_article(self, title: str | None, url: str, summary: str | None = None,
                    raw_markdown: str | None = None, source: str | None = None,
                    topic_tags: list[str] | None = None,
                    published_at: str | None = None) -> dict | None:
        conn = self.connect()
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO articles
                   (title, url, summary, raw_markdown, source, topic_tags, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (title, url, summary, raw_markdown, source,
                 json.dumps(topic_tags or []), published_at)
            )
            conn.commit()
            if cur.lastrowid:
                return self.get_article(cur.lastrowid)
            # Article already exists — return existing
            existing = conn.execute(
                "SELECT * FROM articles WHERE url = ?", (url,)
            ).fetchone()
            return dict(existing) if existing else None
        except Exception as e:
            print(f"[DB] add_article error: {e}")
            return None

    def get_article(self, article_id: int) -> dict | None:
        conn = self.connect()
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        return dict(row) if row else None

    def list_articles(self, status: str | None = None,
                      limit: int = 50) -> list[dict]:
        conn = self.connect()
        query = "SELECT * FROM articles"
        params = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY score DESC, scraped_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(query, params).fetchall()]

    def update_article_score(self, article_id: int, score: int) -> None:
        conn = self.connect()
        conn.execute("UPDATE articles SET score = ? WHERE id = ?",
                     (score, article_id))
        conn.commit()

    # ── Expenses ─────────────────────────────────────────────────────────────

    def add_expense(self, type_: str, amount: float, currency: str = "INR",
                    category: str = "Other", merchant: str | None = None,
                    description: str | None = None,
                    occurred_at: str | None = None,
                    source: str = "manual",
                    message_id: str | None = None) -> dict:
        conn = self.connect()
        cur = conn.execute(
            """INSERT INTO expenses (type, amount, currency, category, merchant,
               description, occurred_at, source, message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (type_, amount, currency, category, merchant,
             description, occurred_at, source, message_id)
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM expenses WHERE id = ?", (cur.lastrowid,)
        ).fetchone())

    def list_expenses(self, type_: str | None = None,
                      from_date: str | None = None,
                      to_date: str | None = None,
                      limit: int = 50) -> list[dict]:
        conn = self.connect()
        query = "SELECT * FROM expenses WHERE 1=1"
        params = []
        if type_:
            query += " AND type = ?"
            params.append(type_)
        if from_date:
            query += " AND occurred_at >= ?"
            params.append(from_date)
        if to_date:
            query += " AND occurred_at <= ?"
            params.append(to_date)
        query += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(query, params).fetchall()]

    def expense_summary(self, from_date: str | None = None,
                        to_date: str | None = None) -> dict:
        conn = self.connect()
        query = """
            SELECT type, COALESCE(SUM(amount), 0) as total
            FROM expenses WHERE 1=1
        """
        params = []
        if from_date:
            query += " AND occurred_at >= ?"
            params.append(from_date)
        if to_date:
            query += " AND occurred_at <= ?"
            params.append(to_date)
        query += " GROUP BY type"
        rows = conn.execute(query, params).fetchall()
        income = sum(r["total"] for r in rows if r["type"] == "income")
        expense = sum(r["total"] for r in rows if r["type"] == "expense")
        return {"income": income, "expense": expense, "net": income - expense}

    # ── Mail Messages ────────────────────────────────────────────────────────

    def upsert_mail(self, msg_id: str, thread_id: str | None = None,
                    from_addr: str | None = None, from_name: str | None = None,
                    subject: str | None = None, snippet: str | None = None,
                    body: str | None = None,
                    internal_date: int | None = None,
                    labels: list[str] | None = None) -> bool:
        """Returns True if this is a NEW message (inserted, not updated)."""
        conn = self.connect()
        existing = conn.execute(
            "SELECT id FROM mail_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        conn.execute(
            """INSERT OR REPLACE INTO mail_messages
               (id, thread_id, from_addr, from_name, subject, snippet, body,
                internal_date, labels, processed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, thread_id, from_addr, from_name, subject, snippet, body,
             internal_date, json.dumps(labels or []),
             0 if existing is None else None)
        )
        conn.commit()
        return existing is None

    def list_mail(self, limit: int = 20) -> list[dict]:
        conn = self.connect()
        return [dict(r) for r in conn.execute(
            "SELECT * FROM mail_messages ORDER BY internal_date DESC LIMIT ?",
            (limit,)
        ).fetchall()]

    def mark_mail_processed(self, msg_id: str) -> None:
        conn = self.connect()
        conn.execute("UPDATE mail_messages SET processed = 1 WHERE id = ?", (msg_id,))
        conn.commit()

    def get_unprocessed_mail(self, limit: int = 50) -> list[dict]:
        conn = self.connect()
        return [dict(r) for r in conn.execute(
            "SELECT * FROM mail_messages WHERE processed = 0 LIMIT ?", (limit,)
        ).fetchall()]

    # ── Settings ─────────────────────────────────────────────────────────────

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        conn = self.connect()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        conn = self.connect()
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        conn.commit()


# ── Module-level singleton ────────────────────────────────────────────────

_db_instance: Database | None = None


def get_db() -> Database:
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
        _db_instance.init_schema()
    return _db_instance


def init_db():
    """Initialize the database (call once at startup)."""
    db = get_db()
    db.init_schema()
    return db
