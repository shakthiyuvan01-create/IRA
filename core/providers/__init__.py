"""providers -- unified AI provider layer for IRA.

Usage:
    from core.providers import AI
    text = AI.generate("prompt", system="...", model="gpt-4o")
    result = AI.chat([{"role": "user", "content": "hi"}])
    json_data = AI.generate_json("generate config", ...)
"""
from .manager import AI, ProviderManager

__all__ = ["AI", "ProviderManager"]
