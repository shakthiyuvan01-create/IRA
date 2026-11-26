"""Composio adapter used by IRA's native tool router.

IRA exposes the whole Composio project behind one tool: every app that has an
ACTIVE connected account (Gmail, Calendar, Drive, Docs, Sheets, GitHub, Reddit,
...) is discovered live from the Composio API, so connecting a new app in the
Composio dashboard makes it usable in IRA without editing code or .env.

Composio stores the OAuth credentials on its side. IRA only keeps the project
API key in the process environment and passes tool arguments and results.

Environment:
    COMPOSIO_API_KEY    Project API key (required).
    COMPOSIO_TOOLKITS   "all" (default) or a comma list that limits which apps
                        IRA may use, e.g. gmail,googledrive.
    COMPOSIO_USER_IDS   "all" (default) or a comma list of Composio user ids
                        whose connected accounts IRA may use.
    COMPOSIO_USER_ID    User id new connections are created under
                        (default ira-local-user).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any


_DEFAULT_USER_ID = "ira-local-user"
_ACCOUNTS_TTL_SECONDS = 60.0
_RESULT_LIMIT = 12000
_SLUG_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)+")

_lock = threading.Lock()
_client = None
_client_key: str | None = None
_accounts_cache: tuple[float, list["_Account"]] | None = None
# Tool slug -> (toolkit slug, version) for every action returned by search.
_discovered: dict[str, tuple[str, str]] = {}


@dataclass(frozen=True)
class _Account:
    id: str
    toolkit: str
    user_id: str
    status: str


# ---------------------------------------------------------------- settings

def _csv_setting(name: str, default: str) -> set[str] | None:
    """Parse a comma list; None means 'all'."""
    raw = os.getenv(name, default).strip()
    if not raw or raw.lower() in {"all", "*"}:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def _allowed_toolkits() -> set[str] | None:
    allowed = _csv_setting("COMPOSIO_TOOLKITS", "all")
    return {item.lower() for item in allowed} if allowed is not None else None


def _allowed_user_ids() -> set[str] | None:
    return _csv_setting("COMPOSIO_USER_IDS", "all")


def _primary_user_id() -> str:
    return os.getenv("COMPOSIO_USER_ID", "").strip() or _DEFAULT_USER_ID


def _toolkit_allowed(toolkit: str) -> bool:
    allowed = _allowed_toolkits()
    return allowed is None or toolkit in allowed


# ---------------------------------------------------------------- client

def _get_client():
    global _client, _client_key, _accounts_cache
    api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Composio is not configured. Add COMPOSIO_API_KEY to IRA's .env file, then restart IRA."
        )
    with _lock:
        if _client is None or _client_key != api_key:
            try:
                from composio import Composio
            except ImportError as exc:
                raise RuntimeError(
                    "Composio SDK is missing. Install IRA requirements with: pip install -r requirements.txt"
                ) from exc
            _client = Composio(api_key=api_key)
            _client_key = api_key
            _accounts_cache = None
            _discovered.clear()
    return _client


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _redact(exc: Exception) -> str:
    return str(exc).replace(os.getenv("COMPOSIO_API_KEY", "\0") or "\0", "[redacted]")


# ---------------------------------------------------------------- accounts

def _fetch_accounts(client) -> list[_Account]:
    accounts: list[_Account] = []
    cursor = None
    for _ in range(50):  # hard stop on runaway pagination
        kwargs: dict[str, Any] = {"limit": 100}
        if cursor:
            kwargs["cursor"] = cursor
        user_ids = _allowed_user_ids()
        if user_ids is not None:
            kwargs["user_ids"] = sorted(user_ids)
        page = client.connected_accounts.list(**kwargs)
        for item in getattr(page, "items", None) or []:
            data = _plain(item)
            toolkit = str((data.get("toolkit") or {}).get("slug") or "").lower()
            if not toolkit or not _toolkit_allowed(toolkit):
                continue
            accounts.append(_Account(
                id=str(data.get("id") or ""),
                toolkit=toolkit,
                user_id=str(data.get("user_id") or ""),
                status=str(data.get("status") or "").upper(),
            ))
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            break
    return accounts


def _accounts(refresh: bool = False) -> list[_Account]:
    global _accounts_cache
    client = _get_client()
    now = time.monotonic()
    cached = _accounts_cache
    if not refresh and cached and now - cached[0] < _ACCOUNTS_TTL_SECONDS:
        return cached[1]
    accounts = _fetch_accounts(client)
    _accounts_cache = (now, accounts)
    return accounts


def _active_by_toolkit() -> dict[str, list[_Account]]:
    grouped: dict[str, list[_Account]] = {}
    for account in _accounts():
        if account.status == "ACTIVE":
            grouped.setdefault(account.toolkit, []).append(account)
    primary = _primary_user_id()
    for items in grouped.values():
        items.sort(key=lambda acc: acc.user_id != primary)  # primary user first
    return grouped


def connected_toolkits() -> list[str]:
    """Slugs of every app with an ACTIVE account IRA may use."""
    return sorted(_active_by_toolkit())


def _account_for(toolkit: str) -> _Account | None:
    items = _active_by_toolkit().get(toolkit)
    return items[0] if items else None


# ---------------------------------------------------------------- tools

def _schema(tool: Any) -> dict:
    data = _plain(tool)
    if not isinstance(data, dict):
        data = {
            "slug": getattr(tool, "slug", None) or getattr(tool, "name", ""),
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", ""),
            "input_parameters": _plain(getattr(tool, "input_parameters", {})),
            "version": getattr(tool, "version", None),
            "toolkit": _plain(getattr(tool, "toolkit", {})),
        }
    toolkit = data.get("toolkit") or {}
    toolkit_slug = toolkit.get("slug") if isinstance(toolkit, dict) else str(toolkit)
    slug = str(data.get("slug") or data.get("name") or "")
    return {
        "slug": slug,
        "toolkit": str(toolkit_slug or slug.split("_", 1)[0]).lower(),
        "name": data.get("name") or slug,
        "description": data.get("description") or "",
        "version": data.get("version") or data.get("toolkit_version") or "latest",
        "arguments_schema": data.get("input_parameters") or data.get("parameters") or {},
    }


def _remember(schema: dict) -> None:
    slug = schema["slug"].upper()
    if slug:
        _discovered[slug] = (schema["toolkit"], str(schema.get("version") or "latest"))


def _resolve_tool(client, slug: str) -> tuple[str, str]:
    """Toolkit and version for a slug, fetching its definition if unseen."""
    if slug not in _discovered:
        _remember(_schema(client.tools.get_raw_composio_tool_by_slug(slug)))
    return _discovered[slug]


def _execute(client, slug: str, arguments: dict, version: str | None = None) -> str:
    toolkit, known_version = _resolve_tool(client, slug)
    if not _toolkit_allowed(toolkit):
        return f"'{toolkit}' is not enabled for IRA. Check COMPOSIO_TOOLKITS in .env."
    account = _account_for(toolkit)
    kwargs: dict[str, Any] = {
        "arguments": arguments,
        "version": version or known_version or "latest",
    }
    if account:
        kwargs["connected_account_id"] = account.id
        kwargs["user_id"] = account.user_id
    else:
        # No-auth toolkits still run; authenticated ones return a clear error.
        kwargs["user_id"] = _primary_user_id()
    if kwargs["version"] == "latest":
        kwargs["dangerously_skip_version_check"] = True
    result = client.tools.execute(slug, **kwargs)
    return json.dumps(_plain(result), ensure_ascii=False, default=str)[:_RESULT_LIMIT]


# ---------------------------------------------------------------- actions

def _status() -> str:
    accounts = _accounts(refresh=True)
    active = _active_by_toolkit()
    stale = sorted({a.toolkit for a in accounts if a.status != "ACTIVE"} - set(active))
    allowed = _allowed_toolkits()
    scope = "all apps" if allowed is None else ", ".join(sorted(allowed))
    if not active:
        lines = [f"Composio is configured (scope: {scope}) but no app is connected yet."]
    else:
        lines = [f"Composio is configured (scope: {scope}). Connected apps:"]
        for toolkit, items in sorted(active.items()):
            suffix = f" ({len(items)} accounts)" if len(items) > 1 else ""
            lines.append(f"- {toolkit}{suffix}")
    if stale:
        lines.append("Needs reconnecting: " + ", ".join(stale) + ". Use action='connect' for each.")
    return "\n".join(lines)


def _connect(client, toolkit: str) -> str:
    if not toolkit:
        return "Name the app to connect, such as googledrive or github."
    if not _toolkit_allowed(toolkit):
        return f"'{toolkit}' is not enabled. Add it to COMPOSIO_TOOLKITS in .env (or set it to all) and restart IRA."
    # A tool-router session issues Connect Links for Composio-managed OAuth,
    # creating the managed auth config on first use.
    session = client.sessions.create(user_id=_primary_user_id(), toolkits=[toolkit])
    request = session.authorize(toolkit)
    url = getattr(request, "redirect_url", None)
    if not url:
        return "Composio did not return a Connect Link. Check this app's auth setup in the Composio dashboard."
    global _accounts_cache
    _accounts_cache = None  # pick the new account up on the next call
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    return f"Complete the {toolkit} sign-in in your browser: {url}"


def _search(client, query: str, toolkit: str) -> str:
    if not query:
        return "Describe the action to find, such as 'list upcoming calendar events'."
    if toolkit:
        if not _toolkit_allowed(toolkit):
            return f"'{toolkit}' is not enabled for IRA. Check COMPOSIO_TOOLKITS in .env."
        selected = [toolkit]
    else:
        selected = connected_toolkits()
        if not selected:
            return "No app is connected in Composio yet. Use action='connect' first."
    tools = client.tools.get_raw_composio_tools(search=query, toolkits=selected, limit=8)
    schemas = [_schema(item) for item in (tools or [])]
    for item in schemas:
        _remember(item)
    connected = set(connected_toolkits())
    for item in schemas:
        item["connected"] = item["toolkit"] in connected
    return json.dumps(schemas, ensure_ascii=False, default=str)


def run_composio(parameters: dict) -> str:
    """Connect an app, find its actions, list connections, or execute an action."""
    try:
        client = _get_client()
        action = str(parameters.get("action", "status")).strip().lower()
        toolkit = str(parameters.get("toolkit", "")).strip().lower()

        if action in {"status", "apps", "list"}:
            return _status()
        if action == "connect":
            return _connect(client, toolkit)
        if action == "search":
            return _search(client, str(parameters.get("query", "")).strip(), toolkit)
        if action == "execute":
            slug = str(parameters.get("tool_slug", "")).strip().upper()
            if not slug or not _SLUG_RE.fullmatch(slug):
                return "Give the exact tool slug returned by Composio search."
            if slug not in _discovered:
                return "Search for that action first; IRA only runs tool slugs returned by Composio search."
            args = parameters.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args or "{}")
            if not isinstance(args, dict):
                return "Composio action arguments must be a JSON object."
            version = str(parameters.get("version", "")).strip() or None
            return _execute(client, slug, args, version)

        return "Unknown Composio action. Use status, connect, search, or execute."
    except Exception as exc:
        return f"Composio error: {_redact(exc)}"


def read_recent_gmail(count: int = 5) -> str:
    """Read message metadata through the connected Composio Gmail account."""
    try:
        client = _get_client()
        if not _toolkit_allowed("gmail"):
            return "Gmail is not enabled in COMPOSIO_TOOLKITS. Add gmail (or set it to all) and restart IRA."
        if not _account_for("gmail"):
            return "Gmail is not connected in Composio. Ask me to connect Gmail."
        count = max(1, min(int(count), 15))
        return _execute(client, "GMAIL_FETCH_EMAILS", {
            "user_id": "me",
            "max_results": count,
            "include_payload": False,
            "verbose": False,
        })
    except Exception as exc:
        return f"Composio Gmail error: {_redact(exc)}"
