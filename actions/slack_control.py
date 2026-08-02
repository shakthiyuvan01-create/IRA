"""
slack_control.py — IRA ↔ Slack (like Hermes' Slack gateway).

- OUTBOUND (just a webhook URL): `slack_send` posts a message to Slack.
- INBOUND remote control (optional): `start_slack_bot` uses Slack Socket Mode
  (needs slack_sdk + a bot token + an app-level token) so messages Yuvan sends
  become commands for IRA.

Config (config/api_keys.json):
    "slack_webhook_url": "https://hooks.slack.com/services/...."   (outbound)
    "slack_bot_token":   "xoxb-..."                                  (inbound)
    "slack_app_token":   "xapp-..."                                  (inbound)
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


CONFIG_PATH  = _base_dir() / "core" / "config" / "api_keys.json"
_bot_started = False


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_placeholder(v: str) -> bool:
    return (not v) or ("REPLACE_WITH" in str(v)) or (not str(v).strip())


def slack_send(parameters=None, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    msg    = (params.get("message") or params.get("text") or "").strip()
    if not msg:
        return "What should I send to Slack, Yuvan?"

    url = _cfg().get("slack_webhook_url", "")
    if _is_placeholder(url):
        return ("Slack isn't set up yet, Yuvan. Add a 'slack_webhook_url' to the config.")
    try:
        r = requests.post(url, json={"text": msg[:3000]}, timeout=10)
        if r.status_code == 200:
            return "Sent to Slack, Yuvan."
        return f"Slack returned status {r.status_code}, Yuvan."
    except Exception as e:
        return f"I couldn't reach Slack, Yuvan: {e}"


def start_slack_bot(on_command) -> None:
    """
    Optional inbound Slack control via Socket Mode. Safe no-op if tokens or
    slack_sdk are missing.
    """
    global _bot_started
    if _bot_started:
        return
    cfg       = _cfg()
    bot_token = cfg.get("slack_bot_token", "")
    app_token = cfg.get("slack_app_token", "")
    if _is_placeholder(bot_token) or _is_placeholder(app_token):
        return

    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web import WebClient
    except Exception:
        print("[Slack] slack_sdk not installed — inbound control disabled. "
              "Run: pip install slack_sdk")
        return

    _bot_started = True

    def _run():
        web    = WebClient(token=bot_token)
        client = SocketModeClient(app_token=app_token, web_client=web)

        def _handle(cli, req):
            try:
                if req.type == "events_api":
                    cli.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
                    event = req.payload.get("event", {})
                    if event.get("type") == "message" and not event.get("bot_id"):
                        text = (event.get("text") or "").strip()
                        ch   = event.get("channel")
                        if text:
                            try:
                                web.chat_postMessage(channel=ch, text="On it, Yuvan… ⚡")
                            except Exception:
                                pass
                            on_command(text)
            except Exception as e:
                print(f"[Slack] handler error: {e}")

        client.socket_mode_request_listeners.append(_handle)
        try:
            client.connect()
            print("[Slack] inbound bot connected.")
            threading.Event().wait()   # keep thread alive
        except Exception as e:
            print(f"[Slack] bot stopped: {e}")

    threading.Thread(target=_run, daemon=True).start()
