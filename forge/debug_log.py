"""Structured debug logging for IRA Forge (ported from Ada-SI)."""

import json
import logging
import os
from typing import Any

logger = logging.getLogger("ira")

MAX_BODY = int(os.environ.get("IRA_LOG_MAX_BODY", "32000"))


def configure_logging() -> None:
    level_name = os.environ.get("IRA_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [IRA-Forge] %(message)s",
        force=True,
    )
    logger.setLevel(level)


def clip(text: str, limit: int | None = None) -> str:
    if not text:
        return ""
    limit = MAX_BODY if limit is None else limit
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [{len(text) - limit} more chars truncated]"


def _prefix(run_id: str, category: str) -> str:
    return f"[run={run_id or '-'}][{category}]"


def log_info(run_id: str, category: str, message: str) -> None:
    logger.info("%s %s", _prefix(run_id, category), message)


def log_debug(run_id: str, category: str, message: str) -> None:
    logger.debug("%s %s", _prefix(run_id, category), message)


def log_warning(run_id: str, category: str, message: str) -> None:
    logger.warning("%s %s", _prefix(run_id, category), message)


def log_error(run_id: str, category: str, message: str) -> None:
    logger.error("%s %s", _prefix(run_id, category), message)


def log_block(
    run_id: str,
    category: str,
    title: str,
    body: str,
    *,
    level: int = logging.INFO,
    limit: int | None = None,
) -> None:
    logger.log(level, "%s %s\n%s", _prefix(run_id, category), title, clip(body, limit))


def log_json(
    run_id: str,
    category: str,
    title: str,
    data: Any,
    *,
    level: int = logging.INFO,
) -> None:
    try:
        body = json.dumps(data, indent=2, default=str)
    except TypeError:
        body = str(data)
    log_block(run_id, category, title, body, level=level)


def log_stream_delta(
    run_id: str,
    source: str,
    kind: str,
    delta: str,
) -> None:
    preview = clip(delta, 400).replace("\n", "\\n")
    logger.debug(
        "%s stream delta kind=%s len=%d: %s",
        _prefix(run_id, f"STREAM/{source}"),
        kind,
        len(delta),
        preview,
    )


def log_chat_request(
    run_id: str,
    *,
    lite_model: str,
    tool_creator_model: str,
    message_count: int,
    tool_names: list[str],
) -> None:
    log_info(
        run_id,
        "CHAT",
        f"request lite_model={lite_model} tool_creator={tool_creator_model or '(none)'} "
        f"messages={message_count} tools={tool_names}",
    )


def log_assistant_turn(
    run_id: str,
    *,
    model: str,
    content: str,
    reasoning: str,
    tool_calls: list[dict],
) -> None:
    log_info(run_id, "LITE_MODEL", f"turn complete model={model}")
    if reasoning:
        log_block(run_id, "REASONING", f"thinking ({len(reasoning)} chars)", reasoning)
    if tool_calls:
        log_json(run_id, "TOOL_CALLS", f"{len(tool_calls)} tool call(s)", tool_calls)
    elif content:
        log_block(run_id, "REPLY", f"assistant reply ({len(content)} chars)", content)
    elif not tool_calls:
        log_info(run_id, "LITE_MODEL", "empty reply (no content, no tool calls)")


def log_tool_execution(
    run_id: str,
    *,
    name: str,
    arguments: dict,
    result: str,
    error: str = "",
) -> None:
    log_json(run_id, "TOOL_CALL", f"execute {name}", {"arguments": arguments})
    if error:
        log_error(run_id, "TOOL_RESULT", f"{name} failed: {error}")
    else:
        log_block(run_id, "TOOL_RESULT", f"{name} returned ({len(result)} chars)", result)


def log_plan(
    run_id: str,
    *,
    tool_name: str,
    plan: str,
    action: str = "drafted",
) -> None:
    log_block(run_id, "PLAN", f"{action} plan for {tool_name} ({len(plan)} chars)", plan)


def log_generated_code(
    run_id: str,
    *,
    tool_name: str,
    tool_code: str,
    test_code: str,
    source: str = "generate",
) -> None:
    log_block(
        run_id,
        "CODE",
        f"{source} tool_code for {tool_name} ({len(tool_code)} chars)",
        tool_code,
    )
    log_block(
        run_id,
        "CODE",
        f"{source} test_code for {tool_name} ({len(test_code)} chars)",
        test_code,
    )


def log_build_event(
    run_id: str,
    *,
    phase: str,
    message: str,
    level: str = "info",
) -> None:
    if level == "error":
        log_error(run_id, f"BUILD/{phase}", message)
    elif level == "warn":
        log_warning(run_id, f"BUILD/{phase}", message)
    else:
        log_info(run_id, f"BUILD/{phase}", message)


def log_runtime_call(
    run_id: str,
    *,
    action: str,
    tool_name: str,
    logs: str = "",
    error: bool = False,
) -> None:
    level = logging.ERROR if error else logging.INFO
    log_block(
        run_id,
        "RUNTIME",
        f"{action} {tool_name}",
        logs,
        level=level,
    )


def log_pip_install(
    run_id: str,
    *,
    packages: list[str],
    logs: str = "",
    approved: bool = False,
    error: bool = False,
) -> None:
    title = f"pip install ({'approved' if approved else 'pending'}): {', '.join(packages)}"
    level = logging.ERROR if error else logging.INFO
    log_block(run_id, "PIP", title, logs, level=level)


def log_sandbox(
    run_id: str,
    *,
    tool_name: str,
    success: bool,
    logs: str,
    attempt: int,
) -> None:
    status = "passed" if success else "failed"
    log_block(
        run_id,
        "SANDBOX",
        f"{tool_name} attempt={attempt + 1} {status} ({len(logs)} chars)",
        logs,
    )
