"""providers/base.py -- Provider interface for IRA's unified AI layer."""
import logging

log = logging.getLogger("providers")


class Provider:
    """Base class. Subclasses implement available() and _generate()."""
    name = "base"
    default_model = ""

    def available(self) -> bool:
        raise NotImplementedError

    def generate(self, prompt: str, system: str = "", model: str = None,
                 max_tokens: int = 1500, temperature: float = 0.3) -> str:
        """Returns generated text. Raises on failure (manager handles fallback)."""
        return self._generate(prompt, system, model or self.default_model,
                              max_tokens, temperature)

    def _generate(self, prompt, system, model, max_tokens, temperature) -> str:
        raise NotImplementedError

    def chat(self, messages: list, model: str = None,
             max_tokens: int = 1500, temperature: float = 0.4) -> str:
        """Multi-turn chat. Default: flatten to (system, user) for providers
        without a native chat endpoint. Overridden by OpenAI-style providers."""
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        turns = []
        for m in messages:
            if m.get("role") == "user":
                turns.append("User: " + m["content"])
            elif m.get("role") == "assistant":
                turns.append("Assistant: " + m["content"])
        prompt = "\n\n".join(turns)
        return self.generate(prompt, system=system, model=model,
                             max_tokens=max_tokens, temperature=temperature)
