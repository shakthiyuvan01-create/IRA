"""Unified streaming LLM client for IRA Forge (ported from Ada-SI).

Provides SSE streaming, tool call extraction, Gemini Google Search grounding,
and JSON Schema sanitization for Vertex AI / Gemini.

This is used by the forge for tool creation prompts (text-only).
IRA's core/llm_client.py continues to be the primary LLM client for voice mode."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# Vertex AI / Gemini function_declarations reject several JSON Schema keywords.
_VERTEX_UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "additionalProperties",
    "$schema",
    "$ref",
    "$defs",
    "definitions",
    "patternProperties",
    "unevaluatedProperties",
    "const",
    "examples",
    "default",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "multipleOf",
    "pattern",
    "format",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minProperties",
    "maxProperties",
})


def sanitize_json_schema_for_vertex(value: Any) -> Any:
    """Recursively strip JSON Schema keys Vertex Gemini tool calling rejects."""
    if isinstance(value, dict):
        return {
            key: sanitize_json_schema_for_vertex(item)
            for key, item in value.items()
            if key not in _VERTEX_UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(value, list):
        return [sanitize_json_schema_for_vertex(item) for item in value]
    return value


def sanitize_tools_for_model(tools: list[dict] | None, model: str) -> list[dict] | None:
    if tools is None or not is_gemini_model(model):
        return tools
    sanitized: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "googleSearch" in tool or "google_search" in tool:
            sanitized.append(tool)
            continue
        sanitized.append(sanitize_json_schema_for_vertex(tool))
    return sanitized


def is_gemini_model(model: str) -> bool:
    return model.startswith("gemini/") or model.startswith("gemini/*")


def merge_search_sources(
    acc: dict[str, dict[str, str]],
    sources: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    for src in sources:
        url = (src.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        title = (src.get("title") or "").strip() or url
        acc[url] = {"title": title, "url": url}
    return acc


def _add_web_source(out: list[dict[str, str]], seen: set[str], web: Any) -> None:
    if not isinstance(web, dict):
        return
    url = (web.get("uri") or web.get("url") or "").strip()
    if not url.startswith("http") or url in seen:
        return
    seen.add(url)
    title = (web.get("title") or "").strip() or url
    out.append({"title": title, "url": url})


def _walk_for_search_sources(obj: Any, out: list[dict[str, str]], seen: set[str]) -> None:
    if isinstance(obj, dict):
        web = obj.get("web")
        if isinstance(web, dict):
            _add_web_source(out, seen, web)
        uri = obj.get("uri") or obj.get("url")
        if uri and (obj.get("title") or obj.get("uri")):
            _add_web_source(out, seen, obj)

        for key in ("groundingChunks", "grounding_chunks", "chunks"):
            chunks = obj.get(key)
            if isinstance(chunks, list):
                for entry in chunks:
                    _walk_for_search_sources(entry, out, seen)

        for key in ("server_side_tool_invocations", "search_results", "results"):
            entries = obj.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    _walk_for_search_sources(entry, out, seen)

        response = obj.get("response")
        if response is not None:
            _walk_for_search_sources(response, out, seen)

        for key in (
            "groundingMetadata",
            "grounding_metadata",
            "vertex_ai_grounding_metadata",
            "provider_specific_fields",
        ):
            nested = obj.get(key)
            if nested is not None:
                _walk_for_search_sources(nested, out, seen)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_search_sources(item, out, seen)


def extract_search_sources_from_chunk(chunk: dict) -> list[dict[str, str]]:
    """Extract deduped web sources from a LiteLLM/Gemini streaming chunk."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    _walk_for_search_sources(chunk, out, seen)

    choices = chunk.get("choices") or []
    if choices:
        choice = choices[0]
        _walk_for_search_sources(choice, out, seen)
        _walk_for_search_sources(choice.get("delta"), out, seen)
        _walk_for_search_sources(choice.get("message"), out, seen)

    return out


def extract_search_sources_from_message(message: dict | None) -> list[dict[str, str]]:
    if not message:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    _walk_for_search_sources(message, out, seen)
    return out


def build_completion_payload(
    model: str,
    messages: list[dict],
    *,
    stream: bool,
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    reasoning_effort: str | None = None,
    gemini_google_search: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
    }
    effective_tools = list(tools) if tools is not None else None
    if gemini_google_search and is_gemini_model(model):
        effective_tools = list(effective_tools or [])
        if not any(
            isinstance(t, dict) and ("googleSearch" in t or "google_search" in t)
            for t in effective_tools
        ):
            effective_tools.append({"googleSearch": {}})
        payload["include_server_side_tool_invocations"] = True
    effective_tools = sanitize_tools_for_model(effective_tools, model)
    if effective_tools is not None:
        payload["tools"] = effective_tools
        payload["tool_choice"] = "auto"
    effort = reasoning_effort
    if effort in ("off", "none"):
        effort = None
    elif effort is None and is_gemini_model(model):
        effort = "low"
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def _thinking_blocks_text(blocks: Any) -> str:
    if not blocks:
        return ""
    if not isinstance(blocks, list):
        return str(blocks)
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            parts.append(block.get("thinking") or block.get("text") or "")
        elif block:
            parts.append(str(block))
    return "".join(parts)


def _coerce_text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        return "".join(
            block.get("text", block) if isinstance(block, dict) else str(block)
            for block in value
        )
    if isinstance(value, str):
        return value
    return str(value)


class ThinkStreamParser:
    """Split reasoning embedded in streamed content tags (e.g. Deepseek/Gemini)."""

    _LT = "<"
    _GT = ">"
    _OPEN = (
        f"{_LT}think{_GT}",
        f"{_LT}redacted_thinking{_GT}",
        f"{_LT}thinking{_GT}",
    )
    _CLOSE = (
        f"{_LT}/think{_GT}",
        f"{_LT}/redacted_thinking{_GT}",
        f"{_LT}/thinking{_GT}",
    )

    def __init__(self) -> None:
        self._in_think = False
        self._carry = ""

    @staticmethod
    def _find_marker(text: str, start: int, markers: tuple[str, ...]) -> tuple[int, int] | None:
        best: tuple[int, int] | None = None
        for marker in markers:
            idx = text.find(marker, start)
            if idx != -1 and (best is None or idx < best[0]):
                best = (idx, len(marker))
        return best

    @staticmethod
    def _split_partial_marker(text: str, markers: tuple[str, ...]) -> tuple[str, str]:
        for marker in markers:
            for size in range(len(marker) - 1, 0, -1):
                suffix = marker[:size]
                if text.endswith(suffix):
                    return suffix, text[:-size]
        return "", text

    def process(self, chunk: str) -> tuple[str, str]:
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        text = self._carry + chunk
        self._carry = ""
        i = 0

        while i < len(text):
            if self._in_think:
                hit = self._find_marker(text, i, self._CLOSE)
                if hit is None:
                    segment = text[i:]
                    self._carry, safe = self._split_partial_marker(segment, self._CLOSE)
                    reasoning_parts.append(safe)
                    break
                reasoning_parts.append(text[i : hit[0]])
                i = hit[0] + hit[1]
                self._in_think = False
            else:
                hit = self._find_marker(text, i, self._OPEN)
                if hit is None:
                    segment = text[i:]
                    self._carry, safe = self._split_partial_marker(segment, self._OPEN)
                    content_parts.append(safe)
                    break
                content_parts.append(text[i : hit[0]])
                i = hit[0] + hit[1]
                self._in_think = True

        return "".join(reasoning_parts), "".join(content_parts)


def extract_stream_delta(
    chunk: dict,
    *,
    think_parser: ThinkStreamParser | None = None,
) -> dict[str, str]:
    """Normalize reasoning and content from a LiteLLM streaming chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return {"reasoning": "", "content": ""}

    choice = choices[0]
    delta_obj = choice.get("delta") or {}

    reasoning = _coerce_text(
        delta_obj.get("reasoning_content")
        or delta_obj.get("reasoning")
        or delta_obj.get("thinking")
        or choice.get("reasoning_content")
        or choice.get("reasoning")
        or choice.get("thinking")
        or ""
    )
    if not reasoning:
        reasoning = _thinking_blocks_text(delta_obj.get("thinking_blocks"))
    if not reasoning:
        reasoning = _thinking_blocks_text(choice.get("thinking_blocks"))

    content = _coerce_text(delta_obj.get("content") or choice.get("text") or "")

    if not reasoning and content and think_parser is not None:
        reasoning, content = think_parser.process(content)

    return {"reasoning": reasoning, "content": content}


def extract_stream_tool_calls(chunk: dict) -> list[dict]:
    choices = chunk.get("choices") or []
    if not choices:
        return []
    delta_obj = choices[0].get("delta") or {}
    return delta_obj.get("tool_calls") or []


def merge_tool_call_delta(acc: dict[int, dict], delta_tool_calls: list[dict]) -> None:
    for tc in delta_tool_calls:
        idx = tc.get("index", 0)
        if idx not in acc:
            acc[idx] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        entry = acc[idx]
        if tc.get("id"):
            entry["id"] = tc["id"]
        if tc.get("type"):
            entry["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            entry["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            entry["function"]["arguments"] += fn["arguments"]


def tool_calls_from_acc(acc: dict[int, dict]) -> list[dict]:
    return [acc[i] for i in sorted(acc) if acc[i].get("id") or acc[i]["function"]["name"]]


def openai_stream_chunk(
    *,
    chunk_id: str,
    reasoning: str = "",
    content: str = "",
    finish_reason: str | None = None,
) -> dict:
    delta: dict[str, str] = {}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


async def stream_chat_completion(
    litellm_url: str,
    headers: dict[str, str],
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    reasoning_effort: str | None = None,
    gemini_google_search: bool = False,
) -> AsyncIterator[dict]:
    payload = build_completion_payload(
        model,
        messages,
        stream=True,
        tools=tools,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        gemini_google_search=gemini_google_search,
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{litellm_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                raise RuntimeError(
                    f"LiteLLM error ({response.status_code}): "
                    f"{body.decode('utf-8', errors='replace')}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    yield json.loads(data_str)
                except json.JSONDecodeError:
                    continue


async def stream_completion_deltas(
    litellm_url: str,
    headers: dict[str, str],
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    reasoning_effort: str | None = None,
    gemini_google_search: bool = False,
) -> AsyncIterator[tuple[str, str]]:
    """Yield (kind, text) where kind is 'reasoning' or 'content'."""
    think_parser = ThinkStreamParser()
    async for chunk in stream_chat_completion(
        litellm_url,
        headers,
        model,
        messages,
        tools=tools,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        gemini_google_search=gemini_google_search,
    ):
        delta = extract_stream_delta(chunk, think_parser=think_parser)
        if delta["reasoning"]:
            yield "reasoning", delta["reasoning"]
        if delta["content"]:
            yield "content", delta["content"]


def new_stream_chunk_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"
