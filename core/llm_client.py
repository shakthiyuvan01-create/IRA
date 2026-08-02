"""
core/llm_client.py — Text-helper LLM client with automatic provider fallback.

Routing model:
  * General conversation (the live voice loop in main.py) → Google Gemini.
  * CODING tasks (code_helper, dev_agent)               → Nara → Bluesminds
                                                            → Gemini (safety net).

This module powers the CODING calls only.  It is NOT used by the main
live-voice loop — that runs on Google Gemini's native-audio Live API, a
Gemini-only protocol that cannot be served by an OpenAI-compatible router.

Provider order (configured in config/api_keys.json → "helper_llm_providers"):

    1. Nara         (https://router.bynara.id/v1)   — PRIMARY
    2. Bluesminds   (OpenAI-compatible router)       — FALLBACK
    3. Gemini       (gemini_api_key)                 — FINAL SAFETY NET

Each provider is tried in order.  If one errors (network, auth, 5xx, empty
response) the next one is tried automatically.  Providers whose API key is
still an unfilled placeholder are silently skipped, so the app keeps working
on the Gemini safety net until real Nara / Bluesminds keys are pasted in.

Config example (config/api_keys.json):

    "helper_llm_providers": [
        {"name": "nara",       "type": "openai",
         "base_url": "https://router.bynara.id/v1",
         "api_key": "sk-nry-...", "model": "mistral-large"},
        {"name": "bluesminds", "type": "openai",
         "base_url": "https://<bluesminds-host>/v1",
         "api_key": "sk-...",     "model": "<model-name>"}
    ],
    "helper_gemini_fallback_model": "gemini-2.5-flash"
"""
import json
import sys
from pathlib import Path

import requests


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = _get_base_dir()
CONFIG_PATH = BASE_DIR / "core" / "config" / "api_keys.json"

_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_placeholder(value: str) -> bool:
    """True if a config value is empty or still an unfilled placeholder."""
    if not value or not str(value).strip():
        return True
    v = str(value).strip().upper()
    markers = ("REPLACE_WITH", "YOUR_", "<", "…", "...", "****")
    return any(m in v for m in markers)


def _get_providers() -> list[dict]:
    """
    Returns the ordered list of usable providers.

    Any OpenAI-compatible provider with a placeholder / empty key or base_url
    is skipped.  A Gemini provider (built from gemini_api_key) is always
    appended last as a final safety net if that key is present.
    """
    cfg       = _load_config()
    providers: list[dict] = []

    for p in cfg.get("helper_llm_providers", []):
        ptype = (p.get("type") or "openai").lower()
        if ptype == "openai":
            if _is_placeholder(p.get("api_key")) or _is_placeholder(p.get("base_url")):
                continue
            providers.append({
                "name":     p.get("name", "openai"),
                "type":     "openai",
                "base_url": p["base_url"].rstrip("/"),
                "api_key":  p["api_key"],
                "model":    p.get("model", ""),
            })
        elif ptype == "gemini":
            if _is_placeholder(p.get("api_key")):
                continue
            providers.append({
                "name":    p.get("name", "gemini"),
                "type":    "gemini",
                "api_key": p["api_key"],
                "model":   p.get("model", _DEFAULT_GEMINI_MODEL),
            })

    # Final safety net: Gemini via the existing gemini_api_key.
    gem_key = cfg.get("gemini_api_key")
    if not _is_placeholder(gem_key):
        already = any(p["type"] == "gemini" and p.get("api_key") == gem_key
                      for p in providers)
        if not already:
            providers.append({
                "name":    "gemini",
                "type":    "gemini",
                "api_key": gem_key,
                "model":   cfg.get("helper_gemini_fallback_model", _DEFAULT_GEMINI_MODEL),
            })

    return providers


def _openai_chat(provider: dict, prompt: str, system: str | None, timeout: int) -> str:
    """One request to an OpenAI-compatible chat-completions endpoint."""
    url = provider["base_url"] + "/chat/completions"
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":    provider["model"],
        "messages": messages,
        "stream":   False,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data   = resp.json()
    choice = (data.get("choices") or [{}])[0]
    text   = (choice.get("message", {}).get("content") or "").strip()
    if not text:
        raise ValueError(f"{provider['name']} returned an empty response.")
    return text


def _gemini_chat(provider: dict, prompt: str, system: str | None, timeout: int) -> str:
    """One request to Google Gemini (safety-net provider)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=provider["api_key"])
    config = types.GenerateContentConfig(system_instruction=system) if system else None
    resp   = client.models.generate_content(
        model=provider["model"],
        contents=prompt,
        config=config,
    )
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def chat(prompt: str, system: str | None = None, timeout: int = 120) -> str:
    """
    Generate text, trying each configured provider in order until one succeeds.

    First tries the new multi-provider system (OmniRoute, GitHub, Gemini, etc.),
    then falls back to the original Nara → Bluesminds → Gemini chain.

    Raises RuntimeError only if EVERY provider fails.
    """
    # Try new multi-provider system first
    try:
        from core.providers import AI
        out = AI.generate(prompt, system=system or "", max_tokens=2000, temperature=0.3)
        if out and not out.startswith("[AI error"):
            return out
    except Exception:
        pass

    # Fall back to original provider chain
    providers = _get_providers()
    if not providers:
        raise RuntimeError(
            "No LLM providers configured. Add 'helper_llm_providers' and/or "
            "'gemini_api_key' to config/api_keys.json."
        )

    errors: list[str] = []
    for p in providers:
        try:
            if p["type"] == "openai":
                out = _openai_chat(p, prompt, system, timeout)
            else:
                out = _gemini_chat(p, prompt, system, timeout)
            if len(providers) > 1 and p is not providers[0]:
                print(f"[LLM] Served by fallback provider '{p['name']}'.")
            return out
        except Exception as e:
            msg = f"{p['name']}: {type(e).__name__}: {e}"
            errors.append(msg)
            print(f"[LLM] Provider '{p['name']}' failed — trying next. ({e})")

    raise RuntimeError("All LLM providers failed:\n  " + "\n  ".join(errors))


# ── Drop-in adapter for modules that expect a Gemini-style model object ──────────

class _Resp:
    """Mimics a Gemini response object: exposes `.text`."""
    def __init__(self, text: str):
        self.text = text


class _LLMModel:
    """
    Drop-in replacement for the tiny Gemini wrapper used by code_helper /
    dev_agent.  Exposes `.generate_content(contents)` returning an object with
    a `.text` attribute, but routes through the Nara → Bluesminds → Gemini
    fallback chain.
    """
    def __init__(self, system: str | None = None):
        self.system = system

    def generate_content(self, contents) -> _Resp:
        if isinstance(contents, str):
            prompt = contents
        elif isinstance(contents, (list, tuple)):
            prompt = "\n".join(str(c) for c in contents)
        else:
            prompt = str(contents)
        return _Resp(chat(prompt, system=self.system))


def make_model(system: str | None = None) -> _LLMModel:
    """Return a Gemini-style model object backed by the fallback chain."""
    return _LLMModel(system)
