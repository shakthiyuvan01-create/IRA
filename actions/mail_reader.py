"""
mail_reader.py — Reads Yuvan's latest Gmail messages over IMAP.

Setup (one time, private & local):
  1. In Gmail, turn on IMAP (Settings → Forwarding and POP/IMAP).
  2. Create an "App password" at https://myaccount.google.com/apppasswords
     (requires 2-Step Verification on the account).
  3. Put these into config/api_keys.json:
        "gmail_address":      "youraddress@gmail.com",
        "gmail_app_password": "the 16-character app password"

The app password is stored locally on your PC only and is never sent anywhere
except to Gmail's own IMAP server.
"""
import email
import imaplib
import json
import sys
from email.header import decode_header
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "core" / "config" / "api_keys.json"


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _decode(value: str) -> str:
    if not value:
        return ""
    out = ""
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                out += text.decode(enc or "utf-8", errors="replace")
            except Exception:
                out += text.decode("utf-8", errors="replace")
        else:
            out += text
    return out.strip()


def _is_placeholder(v: str) -> bool:
    return (not v) or ("REPLACE_WITH" in str(v)) or (not str(v).strip())


def read_emails(parameters=None, response=None, player=None, session_memory=None) -> str:
    """Return a short summary of Yuvan's latest inbox emails (sender + subject)."""
    params = parameters or {}
    try:
        n = int(params.get("count", 5))
    except Exception:
        n = 5
    n = max(1, min(n, 15))

    cfg  = _cfg()
    user = cfg.get("gmail_address", "")
    pw   = cfg.get("gmail_app_password", "")

    if _is_placeholder(user) or _is_placeholder(pw):
        return (
            "Your email isn't set up yet, Yuvan. Add your Gmail address and a "
            "Gmail app password to the config file, then I can read your inbox."
        )

    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com")
        box.login(user, pw)
        box.select("INBOX")
        typ, data = box.search(None, "ALL")
        ids = data[0].split()[-n:]

        results = []
        for msg_id in reversed(ids):
            typ, msg_data = box.fetch(msg_id, "(RFC822.HEADER)")
            if not msg_data or not msg_data[0]:
                continue
            msg  = email.message_from_bytes(msg_data[0][1])
            frm  = _decode(msg.get("From", ""))
            subj = _decode(msg.get("Subject", "")) or "(no subject)"
            results.append(f"From {frm} — {subj}")

        box.close()
        box.logout()

        if not results:
            return "Your inbox looks empty, Yuvan."
        listing = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(results))
        return f"Here are your latest {len(results)} emails, Yuvan:\n{listing}"

    except imaplib.IMAP4.error as e:
        return (
            "I couldn't sign in to your email, Yuvan. Please check that IMAP is "
            f"enabled and the Gmail app password is correct. ({e})"
        )
    except Exception as e:
        return f"Email fetch failed, Yuvan: {e}"
