"""
core/settings_store.py — read/write IRA's user-facing settings.

One place that owns where Settings-panel values live so core/ui.py stays
presentation-only:

  * AI providers  -> config/api_keys.json (Gemini) + .env (next-boot) + live os.environ
  * User profile  -> memory/USER.md (via the MemoryStore, so AI-written entries
                     survive and the snapshot used by the live prompt is rebuilt)

Empty provider fields are treated as "leave unchanged" — a blank key never wipes
an existing one by accident.  Blank profile fields remove that profile entry.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "core" / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"
ENV_FILE   = BASE_DIR / ".env"

# Profile keys written to memory/USER.md as "key: value" entries.
PROFILE_KEYS = ("name", "city", "language", "dob", "favorites", "rules", "news_city")


# ── File helpers ────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    """Atomic write so a crash mid-write can never corrupt api_keys.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ── AI providers ────────────────────────────────────────────────────────────

def load_providers() -> dict:
    """Return the current provider settings shown in the Settings panel."""
    cfg = _read_json(API_FILE)
    return {
        "gemini_api_key":   os.getenv("GEMINI_API_KEY") or cfg.get("gemini_api_key", ""),
        "gemini_model":     os.getenv("GEMINI_MODEL") or cfg.get("gemini_model", "gemini-2.5-flash"),
        "omniroute_api_key": os.getenv("OMNIROUTE_API_KEY", ""),
        "omniroute_url":     os.getenv("OMNIROUTE_URL", "http://localhost:20128/v1"),
        "omniroute_model":   os.getenv("OMNIROUTE_MODEL", "auto"),
    }


def save_providers(updates: dict) -> dict:
    """Persist non-empty provider fields.  Returns {"saved": [...], "errors": [...]}."""
    saved: list[str] = []
    errors: list[str] = []
    cfg = _read_json(API_FILE)
    env: dict[str, str] = {}

    gem_key = (updates.get("gemini_api_key") or "").strip()
    if gem_key:
        cfg["gemini_api_key"] = gem_key
        os.environ["GEMINI_API_KEY"] = gem_key
        saved.append("Gemini API key")

    gem_model = (updates.get("gemini_model") or "").strip()
    if gem_model:
        cfg["gemini_model"] = gem_model
        env["GEMINI_MODEL"] = gem_model
        saved.append("Gemini model")

    omni_key = (updates.get("omniroute_api_key") or "").strip()
    if omni_key:
        env["OMNIROUTE_API_KEY"] = omni_key
        saved.append("OmniRoute API key")

    omni_url = (updates.get("omniroute_url") or "").strip()
    if omni_url:
        env["OMNIROUTE_URL"] = omni_url
        saved.append("OmniRoute URL")

    omni_model = (updates.get("omniroute_model") or "").strip()
    if omni_model:
        env["OMNIROUTE_MODEL"] = omni_model
        saved.append("OmniRoute model")

    try:
        if gem_key or gem_model:
            _write_json(API_FILE, cfg)
    except OSError as e:
        errors.append(f"Could not write api_keys.json: {e}")

    for key, value in env.items():
        os.environ[key] = value
        _set_env_file(key, value)

    return {"saved": saved, "errors": errors}


def _set_env_file(key: str, value: str) -> None:
    """Persist a provider value into .env so it survives restart."""
    try:
        from dotenv import set_key
        set_key(str(ENV_FILE), key, value)
    except Exception:
        # Best-effort — os.environ already carries the live value.
        pass


# ── User profile (memory/USER.md) ───────────────────────────────────────────

def _memory_store():
    """Return the shared MemoryStore with a fresh on-disk view."""
    from core.memory.memory_tool import get_memory_store
    store = get_memory_store()
    try:
        store.load_from_disk()
    except Exception:
        pass
    return store


def load_profile() -> dict:
    """Read the profile fields from USER.md entries ("key: value" blocks)."""
    store = _memory_store()
    result = {k: "" for k in PROFILE_KEYS}
    for entry in store.user_entries:
        key, _, value = entry.partition(":")
        key = key.strip().lower()
        if key in PROFILE_KEYS:
            result[key] = value.strip()
    return result


def save_profile(updates: dict) -> dict:
    """Upsert/remove profile entries atomically.  Returns {"saved": [...], "errors": [...]}.

    Manipulates the entry list directly (keyed by the leading "key:") rather than
    the MemoryStore's substring match — "city" would otherwise collide with
    "news_city" when both carry the same value.
    """
    saved: list[str] = []
    errors: list[str] = []
    store = _memory_store()
    from core.memory.memory_engine import ENTRY_DELIMITER

    try:
        with store._file_lock(store._path_for("user")):
            store.load_from_disk()
            touched: set[str] = set()
            result: list[str] = []
            for entry in store.user_entries:
                key = entry.partition(":")[0].strip().lower()
                if key in PROFILE_KEYS and key in updates:
                    touched.add(key)
                    value = (updates[key] or "").strip()
                    if value:
                        result.append(f"{key}: {value}")
                    # blank value -> drop the entry
                    continue
                result.append(entry)

            # New keys that weren't present get appended at the end.
            for key in PROFILE_KEYS:
                if key in updates and key not in touched:
                    value = (updates[key] or "").strip()
                    if value:
                        result.append(f"{key}: {value}")

            if len(ENTRY_DELIMITER.join(result)) > store.user_char_limit:
                errors.append(f"Profile would exceed the {store.user_char_limit:,} char limit.")
                return {"saved": [], "errors": errors}

            store.user_entries = result
            store.save_to_disk("user")
            # Rebuild the frozen snapshot used by the live system prompt.
            store.load_from_disk()
    except Exception as e:
        errors.append(str(e))
        return {"saved": [], "errors": errors}

    saved = [key for key in PROFILE_KEYS if key in updates]
    return {"saved": saved, "errors": errors}


def get_spoken_language() -> str:
    """Return the saved spoken-language setting ("" if none)."""
    try:
        return load_profile().get("language", "").strip()
    except Exception:
        return ""
