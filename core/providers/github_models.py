"""providers/github_models.py -- GitHub Models (Azure) provider (free GPT-4o)."""
import os
import requests
from .base import Provider

API_URL = "https://models.inference.ai.azure.com/chat/completions"


class GitHubModelsProvider(Provider):
    name = "github"
    default_model = "gpt-4o"

    def available(self) -> bool:
        return bool(os.getenv("GITHUB_TOKEN", ""))

    def _generate(self, prompt, system, model, max_tokens, temperature) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        r = requests.post(
            API_URL,
            headers={"Authorization": "Bearer " + os.getenv("GITHUB_TOKEN", ""),
                     "Content-Type": "application/json"},
            json={"model": model, "messages": msgs,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def chat(self, messages, model=None, max_tokens=1500, temperature=0.4):
        r = requests.post(
            API_URL,
            headers={"Authorization": "Bearer " + os.getenv("GITHUB_TOKEN", ""),
                     "Content-Type": "application/json"},
            json={"model": model or self.default_model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
