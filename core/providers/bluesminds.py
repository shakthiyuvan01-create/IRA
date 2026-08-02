"""providers/bluesminds.py -- BluesMinds gateway provider."""
import os
import requests
from .base import Provider


class BluesMindsProvider(Provider):
    name = "bluesminds"

    @property
    def default_model(self):
        return os.getenv("BLUESMINDS_MODEL", "gpt-4o")

    def available(self) -> bool:
        return bool(os.getenv("BLUESMINDS_KEY", ""))

    def _generate(self, prompt, system, model, max_tokens, temperature) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return self._post(msgs, model, max_tokens, temperature)

    def chat(self, messages, model=None, max_tokens=1500, temperature=0.4):
        return self._post(messages, model, max_tokens, temperature)

    def _post(self, messages, model, max_tokens, temperature) -> str:
        url = os.getenv("BLUESMINDS_BASE_URL",
                        "https://api.bluesminds.ai/v1").rstrip("/")
        r = requests.post(
            url + "/chat/completions",
            headers={"Authorization": "Bearer " + os.getenv("BLUESMINDS_KEY", ""),
                     "Content-Type": "application/json"},
            json={"model": model or self.default_model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
