"""
telegram_control.py — IRA ↔ Telegram (like Hermes' Telegram gateway).

- OUTBOUND: `telegram_send` posts a message to Yuvan's Telegram chat.
- INBOUND remote control: `start_telegram_bot` long-polls the Bot API; messages
  Yuvan sends to the bot become commands for IRA, with an acknowledgement reply.

No extra library needed — pure Telegram Bot HTTP API via requests.

Config (config/api_keys.json):
    "telegram_bot_token": "123456:ABC-..."   (from @BotFather)
    "telegram_chat_id":   "123456789"          (your chat id; optional but
                                                recommended to restrict access)
"""
import json
import sys
import threading
import time
from pathlib import Path

import requests


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH   = _base_dir() / "core" / "config" / "api_keys.json"
_bot_started  = False


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_placeholder(v: str) -> bool:
    return (not v) or ("REPLACE_WITH" in str(v)) or (not str(v).strip())


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def telegram_send(parameters=None, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    msg    = (params.get("message") or params.get("text") or "").strip()
    if not msg:
        return "What should I send to Telegram, Yuvan?"

    cfg   = _cfg()
    token = cfg.get("telegram_bot_token", "")
    chat  = str(params.get("chat_id") or cfg.get("telegram_chat_id", "")).strip()
    if _is_placeholder(token):
        return ("Telegram isn't set up yet, Yuvan. Add a 'telegram_bot_token' "
                "(and 'telegram_chat_id') to the config.")
    if not chat:
        return "I don't have a Telegram chat id to send to, Yuvan. Add 'telegram_chat_id'."
    try:
        r = requests.post(_api(token, "sendMessage"),
                          json={"chat_id": chat, "text": msg[:4000]}, timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            return "Sent to Telegram, Yuvan."
        return f"Telegram error, Yuvan: {r.text[:120]}"
    except Exception as e:
        return f"I couldn't reach Telegram, Yuvan: {e}"


def start_telegram_bot(on_command) -> None:
    """
    Start inbound Telegram control (optional). `on_command(text)` runs each
    message. Safe no-op if the token is missing.
    """
    global _bot_started
    if _bot_started:
        return
    cfg   = _cfg()
    token = cfg.get("telegram_bot_token", "")
    if _is_placeholder(token):
        return
    allow = str(cfg.get("telegram_chat_id", "") or "").strip()
    _bot_started = True

    def _reply(chat_id, text):
        try:
            requests.post(_api(token, "sendMessage"),
                          json={"chat_id": chat_id, "text": text}, timeout=10)
        except Exception:
            pass

    def _loop():
        offset = None
        print("[Telegram] inbound bot polling…")
        while True:
            try:
                params = {"timeout": 30}
                if offset is not None:
                    params["offset"] = offset
                r = requests.get(_api(token, "getUpdates"), params=params, timeout=40)
                data = r.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    if allow and chat_id != allow:
                        continue
                    _reply(chat_id, "On it, Yuvan… ⚡")
                    try:
                        on_command(text)
                    except Exception as e:
                        print(f"[Telegram] command error: {e}")
            except Exception as e:
                print(f"[Telegram] poll error: {e}")
                time.sleep(5)

    threading.Thread(target=_loop, daemon=True).start()
