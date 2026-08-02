"""providers/ollama.py -- Local Ollama provider."""
import os
import requests
from .base import Provider


class OllamaProvider(Provider):
    name = "ollama"

    @property
    def default_model(self):
        return os.getenv("OLLAMA_MODEL", "llama3.2")

    def available(self) -> bool:
        try:
            r = requests.get(os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/tags",
                             timeout=1)
            return r.status_code == 200
        except Exception:
            return False

    def _generate(self, prompt, system, model, max_tokens, temperature) -> str:
        full = (system + "\n\n" + prompt) if system else prompt
        url = os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/generate"
        r = requests.post(
            url,
            json={"model": model or self.default_model, "prompt": full,
                  "stream": False,
                  "options": {"num_predict": max_tokens, "temperature": temperature}},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["response"].strip()
