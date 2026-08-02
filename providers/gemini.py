"""providers/gemini.py -- Google Gemini text provider (NOT the Live Audio API)."""
import os
import requests
from .base import Provider


class GeminiProvider(Provider):
    name = "gemini"

    @property
    def default_model(self):
        return os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    def available(self) -> bool:
        # Try IRA's config/api_keys.json first, then env var
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            try:
                import json
                config_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config", "api_keys.json"
                )
                if os.path.exists(config_path):
                    with open(config_path) as f:
                        data = json.load(f)
                        key = data.get("gemini_api_key", "")
                        if key:
                            os.environ["GEMINI_API_KEY"] = key
            except Exception:
                pass
        return bool(key)

    def _generate(self, prompt, system, model, max_tokens, temperature) -> str:
        if not model or model.startswith(("gpt", "o1", "o3")):
            model = self.default_model
        key = os.getenv("GEMINI_API_KEY", "")
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               + model + ":generateContent?key=" + key)
        contents = [{"parts": [{"text": (system + "\n\n" + prompt) if system else prompt}]}]
        r = requests.post(
            url,
            json={"contents": contents,
                  "generationConfig": {"maxOutputTokens": max_tokens,
                                       "temperature": temperature}},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    def vision(self, prompt: str, image_b64: str, mime: str = "image/jpeg",
               max_tokens: int = 900) -> str:
        """Analyze an image using Gemini vision capabilities."""
        key = os.getenv("GEMINI_API_KEY", "")
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               + self.default_model + ":generateContent?key=" + key)
        r = requests.post(url, json={"contents": [{"parts": [
            {"text": prompt or "Describe this image in detail."},
            {"inline_data": {"mime_type": mime, "data": image_b64}}]}],
            "generationConfig": {"maxOutputTokens": max_tokens}}, timeout=8)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
