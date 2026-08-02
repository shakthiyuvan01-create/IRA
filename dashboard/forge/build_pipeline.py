"""Shared tool build/install pipeline for IRA Forge."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Callable

from dashboard.forge.debug_log import log_build_event, log_pip_install, log_runtime_call
from dashboard.forge.runtime_client import normalize_requirements, runtime_install_tool, runtime_pip_install
from dashboard.forge.tool_verify import (
    augment_requirements_for_missing_module,
    verify_tool_in_ephemeral_venv,
)
from dashboard.forge.tool_creator import (
    fix_runtime_failure,
    fix_test_code,
    validate_test_code,
)
from dashboard.forge.tools_engine import get_new_packages_for_requirements, validate_tool_module, write_tool_files

logger = logging.getLogger(__name__)

PENDING_PIP_INSTALLS: dict[str, dict] = {}
PENDING_UI_PREVIEWS: dict[str, dict] = {}
PIP_TTL_SECONDS = 3600
PHASE_MAX_RETRIES = 3


def cleanup_expired_pip_installs() -> None:
    now = time.time()
    expired = [
        pip_id
        for pip_id, data in PENDING_PIP_INSTALLS.items()
        if now - data.get("created_at", now) > PIP_TTL_SECONDS
    ]
    for pip_id in expired:
        del PENDING_PIP_INSTALLS[pip_id]


def cleanup_expired_ui_previews() -> None:
    now = time.time()
    expired = [
        preview_id
        for preview_id, data in PENDING_UI_PREVIEWS.items()
        if now - data.get("created_at", now) > PIP_TTL_SECONDS
    ]
    for preview_id in expired:
        del PENDING_UI_PREVIEWS[preview_id]


def get_pending_pip(pip_id: str) -> dict:
    cleanup_expired_pip_installs()
    data = PENDING_PIP_INSTALLS.get(pip_id)
    if data is None:
        raise ValueError("Pip install request not found or expired.")
    return data


def get_pending_ui_preview(preview_id: str) -> dict:
    cleanup_expired_ui_previews()
    data = PENDING_UI_PREVIEWS.get(preview_id)
    if data is None:
        raise ValueError("UI preview request not found or expired.")
    return data


async def stream_runtime_install(
    *,
    run_id: str,
    plan_id: str,
    tool_name: str,
    tool_code: str,
    test_code: str,
    requirements: list[str],
    manifest: dict | None = None,
    ui_files: dict[str, str] | None = None,
    new_packages: list[str],
    creator_model: str,
    litellm_url: str,
    litellm_headers: dict[str, str],
    step: Callable[..., str],
    phase: Callable[..., str],
    blog: Callable[..., str],
    sse_data: Callable[[dict], str],
    cancelled: Callable[[], Any],
    skip_pip: bool = False,
    reasoning_effort: str | None = None,
    install_lock: Any = None,
) -> AsyncIterator[str]:
    """Install into tool runtime after pip approval (or when no new packages)."""

    async def _install_body() -> AsyncIterator[str]:
        if new_packages and not skip_pip:
            yield step("pip_review", "Installing approved pip packages", "active")
            yield phase("pip_review", "active")
            pip_log = ""
            pip_error: Exception | None = None
            for pip_attempt in range(PHASE_MAX_RETRIES):
                try:
                    pip_log = await runtime_pip_install(new_packages)
                    pip_error = None
                    break
                except Exception as exc:
                    pip_error = exc
                    if pip_attempt < PHASE_MAX_RETRIES - 1:
                        log_pip_install(
                            run_id,
                            packages=new_packages,
                            logs=str(exc),
                            approved=True,
                            error=True,
                        )
                        yield blog(
                            f"Pip install failed (attempt {pip_attempt + 1}) — retrying…",
                            level="warn",
                        )
                        continue
            if pip_error is not None:
                log_pip_install(
                    run_id, packages=new_packages, logs=str(pip_error), approved=True, error=True
                )
                yield step(
                    "pip_review", "Installing approved pip packages", "error", detail=str(pip_error)
                )
                yield phase("pip_review", "error", detail=str(pip_error))
                yield sse_data(
                    {
                        "ada_event": "tool_build_failed",
                        "run_id": run_id,
                        "tool_name": tool_name,
                        "reason": f"Pip install failed: {pip_error}",
                    }
                )
                yield "data: [DONE]\n\n"
                return
            log_pip_install(run_id, packages=new_packages, logs=pip_log, approved=True)
            yield blog(f"Installed pip packages: {', '.join(new_packages)}")
            yield step("pip_review", "Installing approved pip packages", "done")
            yield phase("pip_review", "done")

        if await cancelled():
            return

        yield step("runtime_verify", "Verifying in tool runtime", "active")
        yield phase("runtime_verify", "active")
        yield blog("Installing tool in persistent runtime and running tests…")

        current_tool = tool_code
        current_test = test_code
        runtime_error: Exception | None = None
        runtime_logs = ""

        for runtime_attempt in range(PHASE_MAX_RETRIES):
            if await cancelled():
                return
            try:
                runtime_logs = await runtime_install_tool(
                    tool_name,
                    current_tool,
                    current_test,
                    requirements,
                    skip_pip=True,
                )
                log_runtime_call(run_id, action="install", tool_name=tool_name, logs=runtime_logs)
                write_tool_files(
                    tool_name,
                    current_tool,
                    requirements,
                    current_test,
                    manifest=manifest,
                    ui_files=ui_files,
                )
                runtime_error = None
                yield blog("Runtime verification passed.")
                break
            except Exception as exc:
                runtime_logs = str(exc)
                runtime_error = exc
                log_runtime_call(
                    run_id, action="install", tool_name=tool_name, logs=runtime_logs, error=True
                )
                if runtime_attempt < PHASE_MAX_RETRIES - 1:
                    yield blog(
                        f"Runtime verification failed (attempt {runtime_attempt + 1}) — "
                        "auto-fixing code/tests…",
                        level="warn",
                    )
                    try:
                        current_tool, current_test = await fix_runtime_failure(
                            tool_name,
                            current_tool,
                            current_test,
                            runtime_logs,
                            creator_model,
                            litellm_url=litellm_url,
                            headers=litellm_headers,
                            run_id=run_id,
                            reasoning_effort=reasoning_effort,
                        )
                        yield sse_data(
                            {
                                "ada_event": "tool_code_ready",
                                "run_id": run_id,
                                "tool_name": tool_name,
                                "tool_code": current_tool,
                                "test_code": current_test,
                                "requirements": requirements,
                            }
                        )
                        continue
                    except Exception as fix_exc:
                        runtime_error = fix_exc
                        break

        if runtime_error is not None:
            yield step(
                "runtime_verify",
                "Verifying in tool runtime",
                "error",
                detail=str(runtime_error)[:500],
            )
            yield phase("runtime_verify", "error", detail=str(runtime_error)[:200])
            yield blog(runtime_logs, level="error")
            yield sse_data(
                {
                    "ada_event": "tool_build_failed",
                    "run_id": run_id,
                    "tool_name": tool_name,
                    "reason": f"Runtime verification failed: {runtime_error}",
                    "logs": runtime_logs,
                }
            )
            yield "data: [DONE]\n\n"
            return

        yield step("runtime_verify", "Verifying in tool runtime", "done")
        yield phase("runtime_verify", "done")

        yield step("install_tool", "Installing tool", "done")
        yield phase("install_tool", "done")
        yield blog("Tool installed successfully.")
        log_build_event(
            run_id,
            phase="install_tool",
            message=f"installed {tool_name} via tool runtime",
        )
        yield sse_data(
            {
                "ada_event": "tool_installed",
                "run_id": run_id,
                "tool_name": tool_name,
                "message": f"Tool '{tool_name}' installed in the persistent tool runtime.",
            }
        )
        yield "data: [DONE]\n\n"

    if install_lock is not None:
        async with install_lock:
            async for event in _install_body():
                yield event
    else:
        async for event in _install_body():
            yield event


async def maybe_pause_for_pip_approval(
    *,
    run_id: str,
    plan_id: str,
    tool_name: str,
    tool_code: str,
    test_code: str,
    requirements: list[str],
    manifest: dict | None = None,
    ui_files: dict[str, str] | None = None,
    creator_model: str,
    step: Callable[..., str],
    phase: Callable[..., str],
    sse_data: Callable[[dict], str],
    reasoning_effort: str | None = None,
    batch_id: str | None = None,
) -> tuple[bool, AsyncIterator[str] | None, list[str]]:
    """Return (paused, pause_event_stream, new_packages)."""
    new_packages, already_installed = await get_new_packages_for_requirements(requirements)
    if not new_packages:
        return False, None, []

    pip_id = uuid.uuid4().hex
    PENDING_PIP_INSTALLS[pip_id] = {
        "pip_id": pip_id,
        "run_id": run_id,
        "plan_id": plan_id,
        "tool_name": tool_name,
        "packages": new_packages,
        "already_installed": already_installed,
        "tool_code": tool_code,
        "test_code": test_code,
        "requirements": requirements,
        "manifest": manifest,
        "ui_files": ui_files,
        "creator_model": creator_model,
        "reasoning_effort": reasoning_effort,
        "batch_id": batch_id or "",
        "created_at": time.time(),
    }

    async def _pause_events() -> AsyncIterator[str]:
        yield step(
            "pip_review",
            "Awaiting pip install approval",
            "active",
            detail=", ".join(new_packages),
        )
        yield phase("pip_review", "active")
        yield sse_data(
            {
                "ada_event": "pip_install_pending",
                "pip_id": pip_id,
                "run_id": run_id,
                "plan_id": plan_id,
                "tool_name": tool_name,
                "packages": new_packages,
                "already_installed": already_installed,
            }
        )
        yield "data: [DONE]\n\n"

    return True, _pause_events(), new_packages


async def maybe_pause_for_ui_preview(
    *,
    run_id: str,
    plan_id: str,
    tool_name: str,
    tool_code: str,
    test_code: str,
    requirements: list[str],
    manifest: dict | None,
    ui_files: dict[str, str] | None = None,
    creator_model: str,
    step: Callable[..., str],
    phase: Callable[..., str],
    sse_data: Callable[[dict], str],
    blog: Callable[..., str],
    reasoning_effort: str | None = None,
    batch_id: str | None = None,
) -> tuple[bool, AsyncIterator[str] | None]:
    """Return (paused, pause_event_stream) for interactive skills after sandbox pass."""
    if not manifest or manifest.get("kind") != "interactive":
        return False, None

    preview_id = uuid.uuid4().hex
    PENDING_UI_PREVIEWS[preview_id] = {
        "preview_id": preview_id,
        "run_id": run_id,
        "plan_id": plan_id,
        "tool_name": tool_name,
        "tool_code": tool_code,
        "test_code": test_code,
        "requirements": requirements,
        "manifest": manifest,
        "ui_files": ui_files,
        "creator_model": creator_model,
        "reasoning_effort": reasoning_effort,
        "batch_id": batch_id or "",
        "preview_installed": False,
        "created_at": time.time(),
    }

    async def _pause_events() -> AsyncIterator[str]:
        preview_data = PENDING_UI_PREVIEWS[preview_id]
        yield step(
            "ui_preview",
            "Awaiting app preview approval",
            "active",
            detail=tool_name,
        )
        yield phase("ui_preview", "active")
        yield blog(f"Installing preview of interactive skill '{tool_name}'…")
        try:
            await runtime_install_tool(
                tool_name,
                tool_code,
                test_code,
                requirements,
                skip_pip=True,
            )
            write_tool_files(
                tool_name,
                tool_code,
                requirements,
                test_code,
                manifest=manifest,
                ui_files=ui_files,
            )
            preview_data["preview_installed"] = True
            yield blog("Preview installed — open the app and try it before approving.")
        except Exception as exc:
            del PENDING_UI_PREVIEWS[preview_id]
            yield step(
                "ui_preview",
                "Awaiting app preview approval",
                "error",
                detail=str(exc)[:200],
            )
            yield phase("ui_preview", "error", detail=str(exc)[:200])
            yield sse_data(
                {
                    "ada_event": "tool_build_failed",
                    "run_id": run_id,
                    "tool_name": tool_name,
                    "reason": f"Preview install failed: {exc}",
                }
            )
            yield "data: [DONE]\n\n"
            return

        yield sse_data(
            {
                "ada_event": "ui_preview_pending",
                "preview_id": preview_id,
                "run_id": run_id,
                "plan_id": plan_id,
                "tool_name": tool_name,
            }
        )
        yield sse_data(
            {
                "ada_event": "preview_skill_app",
                "run_id": run_id,
                "skill_name": tool_name,
            }
        )
        yield "data: [DONE]\n\n"

    return True, _pause_events()


async def run_sandbox_phase(
    *,
    run_id: str,
    tool_name: str,
    tool_code: str,
    test_code: str,
    requirements: list[str],
    manifest: dict | None = None,
    creator_model: str,
    litellm_url: str,
    headers: dict[str, str],
    step: Callable[..., str],
    phase: Callable[..., str],
    blog: Callable[..., str],
    sse_data: Callable[[dict], str],
    cancelled: Callable[[], Any],
    reasoning_effort: str | None = None,
) -> tuple[bool, str, str, str, list[str], list[tuple[str, str]]]:
    """Run ephemeral venv verification with auto-retry."""
    log_output = ""
    current_test = test_code
    current_tool = tool_code
    current_requirements = normalize_requirements(requirements)
    notices: list[tuple[str, str]] = []
    for attempt in range(PHASE_MAX_RETRIES):
        if await cancelled():
            return False, log_output, current_test, current_tool, current_requirements, notices

        success, log_output = verify_tool_in_ephemeral_venv(
            tool_name,
            current_tool,
            current_test,
            current_requirements,
            manifest=manifest,
        )
        if success:
            return True, log_output, current_test, current_tool, current_requirements, notices

        updated, missing = augment_requirements_for_missing_module(
            current_requirements,
            log_output,
        )
        if missing and updated != current_requirements:
            current_requirements = updated
            notices.append(
                (
                    "warn",
                    f"Missing module {missing!r} — installing in verify venv and retrying…",
                )
            )
            continue

        if attempt < PHASE_MAX_RETRIES - 1:
            notices.append(
                (
                    "warn",
                    f"Verification failed (attempt {attempt + 1}) — auto-fixing test_code…",
                )
            )
            fix_hint = ""
            if "Read-only file system" in log_output or "Errno 30" in log_output:
                fix_hint = (
                    "\n\nNote: workspace/skill_data should be writable for interactive skills. "
                    "If persistence still fails, mock only non-skill_data filesystem calls."
                )
            elif "AssertionError" in log_output and (
                "Successfully generated" in log_output
                or "/workspace/" in log_output
                or "tts_output_" in log_output
            ):
                fix_hint = (
                    "\n\nNote: run() return values use the /workspace/ prefix. "
                    "Assert /workspace/ paths in results — not host paths like C:/ or the verify staging directory."
                )
            try:
                current_test = await fix_test_code(
                    tool_name,
                    current_tool,
                    current_test,
                    log_output + fix_hint,
                    creator_model,
                    litellm_url=litellm_url,
                    headers=headers,
                    run_id=run_id,
                    reasoning_effort=reasoning_effort,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "fix_test_code failed for %s (attempt %s): %s",
                    tool_name,
                    attempt + 1,
                    exc,
                )
        return False, log_output, current_test, current_tool, current_requirements, notices
    return False, log_output, current_test, current_tool, current_requirements, notices


async def continue_tool_build(
    *,
    run_id: str,
    plan_id: str,
    tool_name: str,
    tool_code: str,
    test_code: str,
    requirements: list[str],
    manifest: dict | None,
    ui_files: dict[str, str] | None = None,
    creator_model: str,
    litellm_url: str,
    litellm_headers: dict[str, str],
    step: Callable[..., str],
    phase: Callable[..., str],
    blog: Callable[..., str],
    sse_data: Callable[[dict], str],
    cancelled: Callable[[], Any],
    reasoning_effort: str | None = None,
    preview_already_installed: bool = False,
    install_lock: Any = None,
    batch_id: str | None = None,
) -> AsyncIterator[str]:
    paused, pause_events, new_packages = await maybe_pause_for_pip_approval(
        run_id=run_id, plan_id=plan_id, tool_name=tool_name,
        tool_code=tool_code, test_code=test_code, requirements=requirements,
        manifest=manifest, ui_files=ui_files,
        creator_model=creator_model, step=step, phase=phase,
        sse_data=sse_data, reasoning_effort=reasoning_effort, batch_id=batch_id,
    )
    if paused and pause_events:
        async for event in pause_events:
            yield event
        return

    if preview_already_installed:
        yield step("runtime_verify", "Verifying in tool runtime", "done")
        yield phase("runtime_verify", "done")
        yield step("install_tool", "Installing tool", "done")
        yield phase("install_tool", "done")
        yield blog("Tool installed successfully.")
        yield sse_data({
            "ada_event": "tool_installed", "run_id": run_id, "tool_name": tool_name,
            "message": f"Tool '{tool_name}' installed in the persistent tool runtime.",
        })
        yield "data: [DONE]\n\n"
        return

    async for event in stream_runtime_install(
        run_id=run_id, plan_id=plan_id, tool_name=tool_name,
        tool_code=tool_code, test_code=test_code, requirements=requirements,
        manifest=manifest, ui_files=ui_files, new_packages=new_packages,
        creator_model=creator_model, litellm_url=litellm_url,
        litellm_headers=litellm_headers, step=step, phase=phase, blog=blog,
        sse_data=sse_data, cancelled=cancelled, skip_pip=True,
        reasoning_effort=reasoning_effort, install_lock=install_lock,
    ):
        yield event
