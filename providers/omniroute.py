"""providers/omniroute.py -- OmniRoute gateway (OpenAI-compatible).

OmniRoute fronts 237+ providers behind one OpenAI-compatible endpoint with its
own auto-fallback + compression. Env:
  OMNIROUTE_API_KEY   (required)
  OMNIROUTE_URL       (default https://api.omniroute.online/v1)
  OMNIROUTE_MODEL     (default "auto" -- OmniRoute picks the best free provider)
"""
import os
import requests
from .base import Provider


class OmniRouteProvider(Provider):
    name = "omniroute"

    @property
    def default_model(self):
        return os.getenv("OMNIROUTE_MODEL", "auto")

    def available(self) -> bool:
        return bool(os.getenv("OMNIROUTE_API_KEY", ""))

    def _map_model(self, model):
        return model or self.default_model

    def _generate(self, prompt, system, model, max_tokens, temperature) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return self._post(msgs, model, max_tokens, temperature)

    def chat(self, messages, model=None, max_tokens=1500, temperature=0.4):
        return self._post(messages, model, max_tokens, temperature)

    def _post(self, messages, model, max_tokens, temperature) -> str:
        url = os.getenv("OMNIROUTE_URL", "https://api.omniroute.online/v1").rstrip("/")
        r = requests.post(
            url + "/chat/completions",
            headers={"Authorization": "Bearer " + os.getenv("OMNIROUTE_API_KEY", ""),
                     "Content-Type": "application/json"},
            json={"model": self._map_model(model), "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
