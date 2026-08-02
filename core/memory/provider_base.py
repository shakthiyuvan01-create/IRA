"""
memory/provider_base.py — Abstract base class for pluggable memory providers.

External providers (Honcho, Mem0, Supermemory, etc.) can be registered
and managed via the MemoryManager. Only one external provider runs at a
time.

Lifecycle:
  initialize()          — connect, create resources, warm up
  system_prompt_block() — static text for the system prompt
  prefetch(query)       — background recall before each turn
  sync_turn(user, asst) — async write after each turn
  shutdown()            — clean exit

Designed after Hermes Agent's agent/memory_provider.py by Nous Research.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'builtin', 'honcho', 'mem0')."""

    # -- Core lifecycle --------------------------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured, has credentials, and is ready.

        Called during agent init. Should not make network calls — just check config.
        """

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session. Called once at startup."""

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt. Return empty string to skip."""
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn. Return formatted text or empty string."""
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn. Default is no-op."""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist a completed turn. Should be non-blocking."""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas this provider exposes. Return empty list if none."""
        return []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Handle a tool call for one of this provider's tools. Must return JSON string."""
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""

    # -- Optional hooks --------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn."""

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Called when a session ends (explicit exit or timeout)."""

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        """Called when the agent switches session_id mid-process."""

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Called before context compression. Return text to include in summary."""
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called when the built-in memory tool writes an entry."""

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Return config fields this provider needs for setup."""
        return []

    def save_config(self, values: Dict[str, Any]) -> None:
        """Write non-secret config. Providers with native config files should override."""


class BuiltinMemoryProvider(MemoryProvider):
    """
    Built-in MemoryProvider that wraps MemoryStore.

    This is always registered first. Only one external provider is allowed
    alongside it.
    """

    def __init__(self, memory_dir=None):
        self._store = None
        self._memory_dir = memory_dir

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return True  # Built-in is always available

    def initialize(self, session_id: str, **kwargs) -> None:
        from core.memory.memory_engine import create_memory_store
        self._store = create_memory_store(memory_dir=self._memory_dir)
        # Register with memory_tool module
        from core.memory import memory_tool as mt
        mt.reset_memory_store(self._store)

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        return self._store.format_all_for_system_prompt()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._store:
            return ""
        return self._store.format_all_for_system_prompt()

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs) -> None:
        pass  # Built-in stores synced via direct tool calls

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []  # Tool schemas handled by memory_tool.py directly

    def shutdown(self) -> None:
        self._store = None


class MemoryManager:
    """Orchestrates the built-in provider plus at most one external provider.

    The builtin provider is always first. Only one external provider is allowed.
    Failures in one provider never block the other.
    """

    def __init__(self):
        self._providers: List[MemoryProvider] = []
        self._has_external: bool = False

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a memory provider. Only one external provider allowed."""
        is_builtin = provider.name == "builtin"

        if not is_builtin:
            if self._has_external:
                existing = next(
                    (p.name for p in self._providers if p.name != "builtin"), "unknown"
                )
                logger.warning(
                    "Rejected memory provider '%s' — '%s' already registered. "
                    "Only one external provider allowed.",
                    provider.name, existing,
                )
                return
            self._has_external = True

        self._providers.append(provider)
        logger.info("Memory provider '%s' registered", provider.name)

    @property
    def providers(self) -> List[MemoryProvider]:
        return list(self._providers)

    def get_provider(self, name: str) -> Optional[MemoryProvider]:
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def initialize_all(self, session_id: str, **kwargs) -> None:
        for provider in self._providers:
            try:
                provider.initialize(session_id=session_id, **kwargs)
            except Exception as e:
                logger.warning("Provider '%s' initialize failed: %s", provider.name, e)

    def build_system_prompt(self) -> str:
        blocks = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    blocks.append(block)
            except Exception as e:
                logger.warning("Provider '%s' system_prompt_block failed: %s", provider.name, e)
        return "\n\n".join(blocks)

    def prefetch_all(self, query: str) -> str:
        parts = []
        for provider in self._providers:
            try:
                result = provider.prefetch(query)
                if result and result.strip():
                    parts.append(result)
            except Exception:
                pass
        return "\n\n".join(parts)

    def sync_all(self, user_content: str, assistant_content: str, **kwargs) -> None:
        for provider in self._providers:
            try:
                provider.sync_turn(user_content, assistant_content, **kwargs)
            except Exception as e:
                logger.debug("Provider '%s' sync_turn failed: %s", provider.name, e)

    def shutdown_all(self) -> None:
        for provider in reversed(self._providers):
            try:
                provider.shutdown()
            except Exception as e:
                logger.warning("Provider '%s' shutdown failed: %s", provider.name, e)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        for provider in self._providers:
            try:
                provider.on_session_end(messages)
            except Exception:
                pass

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        for provider in self._providers:
            if provider.name == "builtin":
                continue
            try:
                provider.on_memory_write(action, target, content, metadata=metadata)
            except Exception:
                pass