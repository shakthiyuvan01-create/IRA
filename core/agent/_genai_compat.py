"""Thin google-genai compatibility layer for the agent subsystem.

Maps the deprecated `google.generativeai` surface used by the agent modules
(genai.configure / GenerativeModel / generate_content / response.text) onto
the current `google.genai` client, so the modules keep working without the
legacy SDK.
"""
from __future__ import annotations

from typing import Any

from google import genai as _genai
from google.genai import types as _types

_client: _genai.Client | None = None


def configure(api_key: str | None) -> None:
    global _client
    _client = _genai.Client(api_key=api_key)


class _Model:
    """Minimal GenerativeModel stand-in backed by google.genai."""

    def __init__(self, model_name: str, system_instruction: str | None = None):
        self._name = model_name
        self._sys = system_instruction

    def generate_content(self, prompt: str) -> Any:
        if _client is None:
            raise RuntimeError("genai.configure() was not called")
        config = None
        if self._sys:
            config = _types.GenerateContentConfig(system_instruction=self._sys)
        return _client.models.generate_content(
            model=self._name,
            contents=prompt,
            config=config,
        )


def GenerativeModel(model_name: str, system_instruction: str | None = None) -> _Model:
    """Stand-in for google.generativeai.GenerativeModel."""
    return _Model(model_name, system_instruction)
