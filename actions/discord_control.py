"""
discord_control.py — IRA ↔ Discord.

Two capabilities:
  1. OUTBOUND (works with just a webhook URL): `discord_send` posts a message
     to a Discord channel via a webhook. No bot needed.
  2. INBOUND remote control (needs a bot token + `pip install discord.py`):
     `start_discord_bot` runs a bot that turns messages Yuvan sends in Discord
     into commands for IRA, and replies with an acknowledgement.

Config (config/api_keys.json):
    "discord_webhook_url": "https://discord.com/api/webhooks/...."   (outbound)
    "discord_bot_token":   "your-bot-token"                          (inbound)
    "discord_channel_id":  "123456789012345678"   (optional: restrict inbound)
"""
import json
import sys
import threading
from pathlib import Path

import requests


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"
_bot_started = False


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_placeholder(v: str) -> bool:
    return (not v) or ("REPLACE_WITH" in str(v)) or (not str(v).strip())


def discord_send(parameters=None, response=None, player=None, session_memory=None) -> str:
    """Send a message to Discord via the configured webhook."""
    params = parameters or {}
    msg    = (params.get("message") or params.get("text") or "").strip()
    if not msg:
        return "What should I send to Discord, Yuvan?"

    url = _cfg().get("discord_webhook_url", "")
    if _is_placeholder(url):
        return ("Discord isn't set up yet, Yuvan. Add a 'discord_webhook_url' "
                "to the config to let me send messages.")
    try:
        r = requests.post(url, json={"content": msg[:1900]}, timeout=10)
        if r.status_code in (200, 204):
            return "Sent to Discord, Yuvan."
        return f"Discord returned status {r.status_code}, Yuvan."
    except Exception as e:
        return f"I couldn't reach Discord, Yuvan: {e}"


def start_discord_bot(on_command) -> None:
    """
    Start the inbound Discord bot (optional). `on_command(text)` is called with
    each message Yuvan sends in the allowed channel. Safe no-op if the token or
    the discord library is missing.
    """
    global _bot_started
    if _bot_started:
        return

    cfg   = _cfg()
    token = cfg.get("discord_bot_token", "")
    if _is_placeholder(token):
        return  # inbound control not configured — silently skip

    try:
        import discord
    except Exception:
        print("[Discord] discord.py not installed — inbound control disabled. "
              "Run: pip install discord.py")
        return

    channel_id = str(cfg.get("discord_channel_id", "") or "").strip()
    _bot_started = True

    def _run():
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"[Discord] Bot online as {client.user}")

        @client.event
        async def on_message(message):
            if message.author == client.user:
                return
            if channel_id and str(message.channel.id) != channel_id:
                return
            text = (message.content or "").strip()
            if not text:
                return
            try:
                await message.channel.send("On it, Yuvan… ⚡")
            except Exception:
                pass
            try:
                on_command(text)
            except Exception as e:
                print(f"[Discord] command error: {e}")

        try:
            client.run(token)
        except Exception as e:
            print(f"[Discord] bot stopped: {e}")

    threading.Thread(target=_run, daemon=True).start()
    print("[Discord] inbound bot starting…")
