"""
memory/memory_engine.py — Core persistent memory engine for IRA.

Two-tier file-backed memory stores:
  - MEMORY.md: agent's personal notes (environment facts, project conventions, tool quirks)
  - USER.md: what IRA knows about the user (preferences, communication style, workflow habits)

Both are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes update files on disk immediately (durable) but do NOT change
the system prompt — this preserves the prefix cache for the entire session.
The snapshot refreshes on the next session start.

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.

Designed after Hermes Agent's MemoryStore (tools/memory_tool.py by Nous Research).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Cross-platform file locking
fcntl = None
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

ENTRY_DELIMITER = "\n§\n"

# Defaults (configurable via MemoryStore constructor)
DEFAULT_MEMORY_CHAR_LIMIT = 2200
DEFAULT_USER_CHAR_LIMIT = 1375
MAX_CONSOLIDATION_FAILURES_PER_TURN = 3


# ---------------------------------------------------------------------------
# Threat scanning integration (imported from separate module)
# ---------------------------------------------------------------------------

def _first_threat_message(content: str, scope: str = "strict") -> Optional[str]:
    """Scan content for threat patterns. Returns error string if blocked."""
    try:
        from core.memory.threat_patterns import first_threat_message as _ftm
        return _ftm(content, scope=scope)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Snapshot sanitization
# ---------------------------------------------------------------------------

_FENCE_TAG_RE = __import__('re').compile(r'</?\s*memory-context\s*>', __import__('re').IGNORECASE)
_INTERNAL_CONTEXT_RE = __import__('re').compile(
    r'<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>',
    __import__('re').IGNORECASE,
)
_INTERNAL_NOTE_RE = __import__('re').compile(
    r'\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*',
    __import__('re').IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes."""
    text = _INTERNAL_CONTEXT_RE.sub('', text)
    text = _INTERNAL_NOTE_RE.sub('', text)
    text = _FENCE_TAG_RE.sub('', text)
    return text


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """
    Bounded curated memory with file persistence. One instance per IRA session.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt injection.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls, persisted to disk.
        Tool responses always reflect this live state.
    """

    def __init__(
        self,
        memory_dir: Optional[Path] = None,
        memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
        user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
    ):
        from pathlib import Path as _Path
        self._memory_dir = memory_dir or _Path(__file__).resolve().parent.parent / "memory"
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}
        self._consolidation_failures = 0

    # -- Path helpers -----------------------------------------------------------

    def _path_for(self, target: str) -> Path:
        if target == "user":
            return self._memory_dir / "USER.md"
        return self._memory_dir / "MEMORY.md"

    def _entries_for(self, target: str) -> List[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: List[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    # -- File I/O ---------------------------------------------------------------

    @contextmanager
    def _file_lock(self, path: Path):
        """Acquire an exclusive file lock for read-modify-write safety.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries.

        No file locking needed: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new complete file.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Write entries to a memory file using atomic temp-file + rename."""
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")

    # -- Drift detection --------------------------------------------------------

    def _detect_external_drift(self, target: str) -> Optional[str]:
        """Return a backup-path string if on-disk content shows external drift.

        The memory file is supposed to be a list of small entries joined by §.
        Detect drift via two signals:
        1. Round-trip mismatch — re-parsing and re-serializing doesn't match
        2. Entry-size overflow — any single entry exceeds the whole-file char limit
        """
        path = self._path_for(target)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return None
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except (OSError, IOError):
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    def _reload_target(self, target: str, *, skip_drift: bool = False) -> Optional[str]:
        """Re-read entries from disk into in-memory state. Returns backup path if drift detected."""
        path = self._path_for(target)
        bak = None if skip_drift else self._detect_external_drift(target)
        fresh = self._read_file(path)
        fresh = list(dict.fromkeys(fresh))  # deduplicate
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str):
        """Persist entries to the appropriate file. Called after every mutation."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    # -- Load / snapshot -------------------------------------------------------

    def load_from_disk(self):
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot.

        The frozen snapshot is what enters the system prompt. Injection/promptware
        scanning is applied at snapshot-build time — any hit replaces the entry
        text with a placeholder.
        """
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(self._memory_dir / "MEMORY.md")
        self.user_entries = self._read_file(self._memory_dir / "USER.md")

        # Deduplicate
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Sanitize for snapshot only (live state keeps raw text)
        sanitized_memory = self._sanitize_entries(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries(self.user_entries, "USER.md")

        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    def _sanitize_entries(self, entries: List[str], filename: str) -> List[str]:
        """Return entries with any threat-matching entry replaced by a placeholder."""
        sanitized: List[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = self._scan_entry(entry)
            if findings:
                logger.warning(
                    "Memory entry from %s blocked at load time: %s",
                    filename, ", ".join(findings),
                )
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Use memory(action=remove) "
                    f"to delete the original.]"
                )
            else:
                sanitized.append(entry)
        return sanitized

    def _scan_entry(self, content: str) -> List[str]:
        """Scan a single entry for threat patterns. Returns list of pattern IDs found."""
        try:
            from core.memory.threat_patterns import scan_for_threats as _sft
            return _sft(content, scope="strict")
        except ImportError:
            return []

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        Return the frozen snapshot for system prompt injection.
        Returns None if empty.
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    def format_all_for_system_prompt(self) -> str:
        """Return both memory and user blocks joined for system prompt."""
        blocks = []
        for target in ("user", "memory"):
            block = self.format_for_system_prompt(target)
            if block:
                blocks.append(block)
        return "\n\n".join(blocks)

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    # -- Operations -------------------------------------------------------------

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        scan_error = _first_threat_message(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target, skip_drift=True)

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate now: use 'replace' to merge overlapping entries or "
                        f"'remove' stale ones (see current_entries below), then retry."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        scan_error = _first_threat_message(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return self._drift_error(target, bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches],
                    }

            idx = matches[0][0]
            limit = self._char_limit(target)

            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content, or 'remove' other entries to make room."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return self._drift_error(target, bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries below.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches],
                    }

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply a sequence of add/replace/remove ops atomically against final budget."""
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        # Scan all add/replace content first
        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            new_content = (op or {}).get("content")
            if act in {"add", "replace"} and new_content:
                scan_error = _first_threat_message(new_content)
                if scan_error:
                    return {"success": False, "error": f"Operation {i + 1}: {scan_error}"}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return self._drift_error(target, bak)

            working: List[str] = list(self._entries_for(target))
            limit = self._char_limit(target)

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    if content in working:
                        continue
                    working.append(content)

                elif act == "replace":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    m = [j for j, e in enumerate(working) if old_text in e]
                    if not m:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in m}) > 1:
                        return self._batch_error(
                            target, f"{pos}: '{old_text}' matched multiple distinct entries."
                        )
                    working[m[0]] = content

                elif act == "remove":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    m = [j for j, e in enumerate(working) if old_text in e]
                    if not m:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in m}) > 1:
                        return self._batch_error(
                            target, f"{pos}: '{old_text}' matched multiple distinct entries."
                        )
                    working.pop(m[0])

                else:
                    return self._batch_error(target, f"{pos}: unknown action. Use add, replace, or remove.")

            # Budget check against FINAL state
            new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"After applying all {len(operations)} operations, memory would be at "
                        f"{new_total:,}/{limit:,} chars. Remove or shorten more entries."
                    ),
                    "current_entries": self._entries_for(target),
                    "usage": f"{current:,}/{limit:,}",
                })

            self._set_entries(target, working)
            self.save_to_disk(target)

        return self._success_response(target, f"Applied {len(operations)} operation(s).")

    # -- Helpers ----------------------------------------------------------------

    def reset_consolidation_failures(self):
        """Reset the per-turn consolidation-failure counter."""
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Count an at-capacity consolidation failure and degrade gracefully."""
        self._consolidation_failures += 1
        if self._consolidation_failures <= MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory consolidation failed {self._consolidation_failures} times "
                "this turn. Stop retrying — leave memory unchanged and continue."
            ),
        }

    def _batch_error(self, target: str, message: str) -> Dict[str, Any]:
        current = self._char_count(target)
        limit = self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_entries": self._entries_for(target),
            "usage": f"{current:,}/{limit:,}",
        })

    def _drift_error(self, target: str, bak_path: str) -> Dict[str, Any]:
        path = self._path_for(target)
        return {
            "success": False,
            "error": (
                f"Refusing to write {path.name}: file on disk has content that "
                f"wouldn't round-trip (likely added externally). "
                f"Backup saved to {bak_path}. "
                f"Resolve the drift first, then retry."
            ),
            "drift_backup": bak_path,
            "remediation": (
                "Restore from the .bak file, or rewrite the file as a clean "
                "§-delimited list of entries."
            ),
        }

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        self._consolidation_failures = 0
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        resp = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    # -- Migration from legacy long_term.json -----------------------------------

    @classmethod
    def migrate_from_json(cls, memory_dir: Optional[Path] = None) -> "MemoryStore":
        """
        Create a MemoryStore from the legacy long_term.json data.
        Reads existing long_term.json, maps categories to MEMORY.md and USER.md entries.
        """
        if memory_dir is None:
            memory_dir = Path(__file__).resolve().parent.parent / "memory"

        store = cls(memory_dir=memory_dir)

        # Check if already migrated (MEMORY.md or USER.md already has content)
        mem_path = memory_dir / "MEMORY.md"
        user_path = memory_dir / "USER.md"
        if mem_path.exists() and mem_path.read_text(encoding="utf-8").strip():
            # Already has data — load it and also merge long_term.json
            existing_memory = store._read_file(mem_path)
            store.memory_entries = list(dict.fromkeys(existing_memory))
        if user_path.exists() and user_path.read_text(encoding="utf-8").strip():
            existing_user = store._read_file(user_path)
            store.user_entries = list(dict.fromkeys(existing_user))

        # Read long_term.json
        lt_path = memory_dir / "long_term.json"
        if not lt_path.exists():
            store.load_from_disk()
            return store

        try:
            data = json.loads(lt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            store.load_from_disk()
            return store

        if not isinstance(data, dict):
            store.load_from_disk()
            return store

        # Map categories to MEMORY.md
        memory_categories = {"projects", "notes", "wishes"}
        user_categories = {"identity", "preferences", "relationships"}

        for cat, items in data.items():
            if not isinstance(items, dict):
                continue
            if cat in memory_categories:
                for key, entry in items.items():
                    val = entry.get("value") if isinstance(entry, dict) else entry
                    if val and str(val).strip():
                        entry_text = f"{key}: {val}"
                        if entry_text not in store.memory_entries:
                            store.memory_entries.append(entry_text)
            elif cat in user_categories:
                for key, entry in items.items():
                    val = entry.get("value") if isinstance(entry, dict) else entry
                    if val and str(val).strip():
                        entry_text = f"{key}: {val}"
                        if entry_text not in store.user_entries:
                            store.user_entries.append(entry_text)

        # Write migrated data
        store.save_to_disk("memory")
        store.save_to_disk("user")

        # Build snapshot
        store._system_prompt_snapshot = {
            "memory": store._render_block("memory", store.memory_entries),
            "user": store._render_block("user", store.user_entries),
        }

        return store

    # -- In-memory access for backward compatibility ----------------------------

    def get_user_identity(self) -> Dict[str, str]:
        """Return user identity fields for backward compatibility."""
        result = {}
        for entry in self.user_entries:
            for prefix in ("name:", "age:", "city:", "job:", "language:", "nationality:"):
                if entry.lower().startswith(prefix):
                    val = entry.split(":", 1)[-1].strip()
                    key = prefix.rstrip(":")
                    result[key] = val
        return result

    def has_entries(self, target: str) -> bool:
        """Check if a target store has entries."""
        return len(self._entries_for(target)) > 0


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_memory_store(memory_dir: Optional[Path] = None, migrate: bool = True) -> MemoryStore:
    """
    Create a MemoryStore, optionally migrating from legacy long_term.json.
    If migration is disabled, loads from MEMORY.md/USER.md directly.
    """
    store = MemoryStore(memory_dir=memory_dir)
    if migrate:
        # Try migration first (checks if already migrated)
        return MemoryStore.migrate_from_json(memory_dir=memory_dir)

    lt_path = (memory_dir or store._memory_dir) / "long_term.json"
    if migrate and lt_path.exists():
        return MemoryStore.migrate_from_json(memory_dir=memory_dir)

    store.load_from_disk()
    return store