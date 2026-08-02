"""
clipboard_manager.py — IRA remembers your clipboard history.

A background watcher records everything Yuvan copies (deduped, capped), stored
locally. IRA can list the history and copy any past item back to the clipboard.
"""
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


HISTORY_PATH   = _base_dir() / "memory" / "clipboard_history.json"
MAX_ITEMS      = 50
_lock          = threading.Lock()
_watch_started = False


def _load() -> list:
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list) -> None:
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    except Exception as e:
        print(f"[Clipboard] save error: {e}")


def _record(text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        items = _load()
        if items and items[0].get("text") == text:
            return                      # skip consecutive duplicate
        items.insert(0, {"text": text[:2000],
                         "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
        _save(items[:MAX_ITEMS])


def start_clipboard_watch() -> None:
    """Start the background clipboard recorder (call once at startup)."""
    global _watch_started
    if _watch_started:
        return
    _watch_started = True

    def _loop():
        try:
            import pyperclip
        except Exception:
            print("[Clipboard] pyperclip not available; history disabled.")
            return
        last = None
        while True:
            try:
                cur = pyperclip.paste()
                if cur and cur != last:
                    last = cur
                    _record(cur)
            except Exception:
                pass
            time.sleep(1.5)

    threading.Thread(target=_loop, daemon=True).start()
    print("[Clipboard] history watcher started.")


def clipboard(parameters=None, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "history").strip().lower()

    if action in ("history", "list", "show"):
        items = _load()
        if not items:
            return "Your clipboard history is empty, Yuvan."
        try:
            n = int(params.get("count", 10))
        except Exception:
            n = 10
        n = max(1, min(n, len(items)))
        lines = [f"{i + 1}. {items[i]['text'][:80].strip()}" for i in range(n)]
        return "Recent clipboard items, Yuvan:\n" + "\n".join(lines)

    if action in ("recall", "get", "copy", "paste"):
        try:
            idx = int(params.get("index", 1)) - 1
        except Exception:
            idx = 0
        items = _load()
        if 0 <= idx < len(items):
            try:
                import pyperclip
                pyperclip.copy(items[idx]["text"])
                return (f"Copied clipboard item {idx + 1} back for you, Yuvan. "
                        "Press Ctrl+V to paste it.")
            except Exception as e:
                return f"I couldn't set the clipboard, Yuvan: {e}"
        return "That clipboard item doesn't exist, Yuvan."

    if action in ("save", "record", "add"):
        try:
            import pyperclip
            _record(pyperclip.paste())
            return "Saved your current clipboard, Yuvan."
        except Exception as e:
            return f"I couldn't read the clipboard, Yuvan: {e}"

    if action in ("clear", "wipe"):
        _save([])
        return "Cleared your clipboard history, Yuvan."

    return "Clipboard actions, Yuvan: history, recall (with index), save, clear."
