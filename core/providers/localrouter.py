"""providers/localrouter.py -- Local OmniRoute-style router (OpenAI-compatible).

This is IRA's FAST primary brain. It fronts Yuvan's self-hosted router at
http://localhost:20128/v1 which routes to 237+ upstream models. Because the
router is on localhost, latency is ~tens of milliseconds -- IRA answers far
faster than any cloud round-trip.

Env (all optional; sane defaults provided):
  LOCALROUTE_API_KEY   (default "sk-local" -- the router accepts any key locally)
  LOCALROUTE_URL       (default http://localhost:20128/v1)
  LOCALROUTE_MODEL     (default "auto" -- the router picks the best backend)

The router streams SSE by default, so we always send "stream": false to
avoid the JSONDecodeError that raw .json() would hit on a streamed body.
"""
import os
import base64
import requests
from .base import Provider


class LocalRouterProvider(Provider):
    name = "local"

    @property
    def default_model(self):
        return os.getenv("LOCALROUTE_MODEL", "auto")

    def available(self) -> bool:
        # The router is local; we treat it as available whenever its URL is
        # reachable-ish. We don't hard-block on a missing key because the local
        # router accepts any key. We only skip if explicitly disabled.
        if os.getenv("LOCALROUTE_DISABLE", "").lower() in ("1", "true", "yes"):
            return False
        return True

    def _base(self) -> str:
        return os.getenv("LOCALROUTE_URL", "http://localhost:20128/v1").rstrip("/")

    def _key(self) -> str:
        return os.getenv("LOCALROUTE_API_KEY", "sk-local")

    def _post(self, messages, model, max_tokens, temperature) -> str:
        url = self._base() + "/chat/completions"
        r = requests.post(
            url,
            headers={"Authorization": "Bearer " + self._key(),
                     "Content-Type": "application/json"},
            json={"model": model or self.default_model,
                  "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature,
                  "stream": False},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def _generate(self, prompt, system, model, max_tokens, temperature) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return self._post(msgs, model, max_tokens, temperature)

    def chat(self, messages, model=None, max_tokens=1500, temperature=0.4):
        return self._post(messages, model, max_tokens, temperature)

    # ── Vision: local router supports image_url parts ───────────────────────
    def vision(self, prompt: str, image_b64: str, mime: str = "image/jpeg",
               max_tokens: int = 900) -> str:
        """Analyze an image via the local router's vision-capable backends."""
        url = self._base() + "/chat/completions"
        data_uri = f"data:{mime};base64,{image_b64}"
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt or "Describe this image in detail."},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }]
        r = requests.post(
            url,
            headers={"Authorization": "Bearer " + self._key(),
                     "Content-Type": "application/json"},
            json={"model": self.default_model, "messages": messages,
                  "max_tokens": max_tokens, "stream": False},
            timeout=25,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
