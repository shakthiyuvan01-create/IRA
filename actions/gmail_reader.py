"""gmail_reader.py — Google OAuth + Gmail inbox reader.

Adapted from agentic-os-personal-main's server/google/oauth.js and server/google/gmail.js.
Requires: google-api-python-client, google-auth-oauthlib, google-auth-httplib2
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime

from core.data.database import get_db


def _get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
TOKEN_PATH = BASE_DIR / "core" / "config" / "gmail_token.json"


def _import_google():
    """Lazy import Google APIs."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        import google.auth.exceptions
        return Request, Credentials, InstalledAppFlow, build, google.auth.exceptions
    except ImportError:
        return None, None, None, None, None


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _get_credentials():
    """Get Gmail API credentials, running OAuth flow if needed."""
    Request, Credentials, InstalledAppFlow, build, GoogleAuthError = _import_google()
    if Request is None:
        return None, "google-api-python-client not installed"

    creds = None

    # Check config for client credentials
    try:
        with open(str(BASE_DIR / "core" / "config" / "api_keys.json"), "r") as f:
            api_config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None, "config/api_keys.json not found"

    client_id = api_config.get("google_client_id", "")
    client_secret = api_config.get("google_client_secret", "")
    if not client_id or not client_secret:
        return None, (
            "Gmail not configured. Add google_client_id and "
            "google_client_secret to config/api_keys.json"
        )

    # Load saved token
    if TOKEN_PATH.exists():
        try:
            with open(str(TOKEN_PATH), "r") as f:
                creds = Credentials.from_authorized_user_info(json.load(f), SCOPES)
        except Exception:
            creds = None

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
        except Exception:
            creds = None

    # Run OAuth flow if needed
    if not creds or not creds.valid:
        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        try:
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True)
            _save_token(creds)
        except Exception as e:
            return None, "OAuth failed: " + str(e)

    return creds, None


def _save_token(creds):
    """Persist credential token to file."""
    try:
        data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(str(TOKEN_PATH), "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("[Gmail] Failed to save token: " + str(e))


def _header(payload, name):
    """Extract a header value from a message payload."""
    headers = payload.get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_from(value):
    """Parse 'Name <email>' format."""
    import re
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', value)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", value.strip()


def list_messages(max_results=10):
    """List recent Gmail messages with metadata."""
    Request, Credentials, InstalledAppFlow, build, GoogleAuthError = _import_google()
    if Request is None:
        return {"success": False, "error": "google-api-python-client not installed"}

    creds, error = _get_credentials()
    if creds is None:
        return {"success": False, "error": error}

    try:
        service = build("gmail", "v1", credentials=creds)
        results = service.users().messages().list(
            userId="me", maxResults=max_results
        ).execute()

        messages = []
        for msg_data in results.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=msg_data["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()

            payload = msg.get("payload", {})
            from_val = _header(payload, "From")
            from_name, from_addr = _parse_from(from_val)
            subject = _header(payload, "Subject")
            snippet = msg.get("snippet", "")
            internal_date = int(msg.get("internalDate", 0))

            messages.append({
                "id": msg["id"],
                "from_name": from_name,
                "from_addr": from_addr,
                "subject": subject,
                "snippet": snippet[:200],
                "date": datetime.fromtimestamp(internal_date / 1000).isoformat()
                if internal_date else None,
            })

            # Store in DB
            db = get_db()
            db.upsert_mail(
                msg_id=msg["id"],
                from_addr=from_addr,
                from_name=from_name,
                subject=subject,
                snippet=snippet[:200],
                internal_date=internal_date,
            )

        return {"success": True, "messages": messages, "count": len(messages)}

    except Exception as e:
        return {"success": False, "error": "Gmail API error: " + str(e)}


def gmail_status():
    """Check if Gmail is configured and connected."""
    creds, error = _get_credentials()
    if creds is None:
        return {"configured": False, "connected": False, "error": error}
    return {"configured": True, "connected": True, "email": "Connected"}


def gmail_reader(parameters=None, response=None, player=None, session_memory=None, speak=None):
    """Tool entry point — read Gmail inbox."""
    params = parameters or {}
    action = params.get("action", "list").strip().lower()
    count = int(params.get("count", 5))

    if action == "status":
        status = gmail_status()
        if status["configured"]:
            return "Gmail is configured and connected, Yuvan."
        return "Gmail is not configured: " + status.get("error", "Unknown")

    if player:
        player.write_log("[Gmail] Listing " + str(count) + " messages")
    if speak:
        speak("Checking your email. One moment, Yuvan.")

    result = list_messages(max_results=count)

    if not result.get("success"):
        return "Failed to read email: " + result.get("error", "Unknown error")

    if not result["messages"]:
        return "No new emails found, Yuvan."

    lines = ["Your recent emails, Yuvan:"]
    for m in result["messages"]:
        from_info = m["from_name"] or m["from_addr"]
        lines.append("- " + m["subject"] + " from " + from_info)

    return "\n".join(lines)
