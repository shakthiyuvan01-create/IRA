"""providers/openai_provider.py -- OpenAI API provider."""
import os
import requests
from .base import Provider


class OpenAIProvider(Provider):
    name = "openai"

    @property
    def default_model(self):
        return os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def available(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY", ""))

    def _generate(self, prompt, system, model, max_tokens, temperature) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return self._chat_complete(msgs, model, max_tokens, temperature)

    def chat(self, messages, model=None, max_tokens=1500, temperature=0.4):
        return self._chat_complete(messages, model or self.default_model,
                                   max_tokens, temperature)

    def _chat_complete(self, messages, model, max_tokens, temperature) -> str:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer " + os.getenv("OPENAI_API_KEY", ""),
                     "Content-Type": "application/json"},
            json={"model": model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
