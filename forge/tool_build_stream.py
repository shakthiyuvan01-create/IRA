"""Shared tool build stream (single-tool and batch) for IRA Forge.

Handles the full codegen → validate → sandbox → QA → preview → install pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from forge.build_pipeline import (
    PHASE_MAX_RETRIES,
    continue_tool_build,
    maybe_pause_for_ui_preview,
    run_sandbox_phase,
)
from forge.build_ui_qa import stream_interactive_ui_qa
from forge.debug_log import log_build_event, log_generated_code, log_sandbox
from forge.tool_creator import (
    fix_validation_errors,
    generate_tool_code_stream,
    parse_generated_tool_response,
    repair_generated_tool_response,
    validate_test_code,
)
from forge.tools_engine import validate_manifest, validate_tool_schema, validate_ui_files


def _tag_payload(
    payload: dict,
    *,
    batch_id: str | None,
    plan_id: str | None,
    tool_name: str | None,
) -> dict:
    data = dict(payload)
    if batch_id:
        data["batch_id"] = batch_id
    if plan_id:
        data["plan_id"] = plan_id
    if tool_name:
        data["tool_name"] = tool_name
    return data


async def stream_tool_build(
    *,
    plan_id: str,
    plan_data: dict,
    run_id: str,
    creator_model: str,
    reasoning_effort: str | None,
    litellm_url: str,
    litellm_headers: dict[str, str],
    pending_plans: dict[str, dict],
    process_step: Callable[..., str],
    tool_build_phase: Callable[..., str],
    tool_build_log: Callable[..., str],
    sse_data: Callable[[dict], str],
    cancelled_events: Callable[..., list[str]],
    is_run_cancelled: Callable[[str], bool],
    clear_run_cancelled: Callable[[str], None],
    cancelled_override: Callable[[], Any] | None = None,
    batch_id: str | None = None,
    install_lock: Any = None,
    on_installed: Callable[[str, str, str], None] | None = None,
    on_failed: Callable[[str, str, str], None] | None = None,
    skip_plan_cleanup: bool = False,
) -> AsyncIterator[str]:
    """Run codegen through install for one pending plan."""
    tool_name = plan_data["tool_name"]

    def step(step_id: str, label: str, status: str, *, detail: str = ""):
        if not run_id:
            return ""
        return process_step(
            run_id,
            step_id,
            label,
            status,
            model=creator_model,
            detail=detail,
        )

    def phase(step_id: str, status: str, *, detail: str = ""):
        if batch_id:
            return emit(
                {
                    "ada_event": "tool_build_phase",
                    "run_id": run_id,
                    "phase": step_id,
                    "status": status,
                    "detail": detail,
                }
            )
        if not run_id:
            return ""
        return tool_build_phase(run_id, step_id, status, detail=detail)

    def blog(message: str, *, level: str = "info"):
        if batch_id:
            return emit(
                {
                    "ada_event": "tool_build_log",
                    "run_id": run_id,
                    "message": message,
                    "level": level,
                }
            )
        if not run_id:
            return ""
        return tool_build_log(run_id, message, level=level)

    def emit(payload: dict) -> str:
        return sse_data(
            _tag_payload(
                payload,
                batch_id=batch_id,
                plan_id=plan_id,
                tool_name=tool_name,
            )
        )

    async def cancelled() -> bool:
        if cancelled_override is not None:
            return await cancelled_override()
        return is_run_cancelled(run_id)

    clear_run_cancelled(run_id)
    log_build_event(
        run_id,
        phase="approve",
        message=(
            f"build started plan_id={plan_id} tool={tool_name} "
            f"creator_model={creator_model} batch_id={batch_id or ''}"
        ),
    )

    if batch_id:
        yield emit(
            {
                "ada_event": "forge_batch_build_started",
                "run_id": run_id,
            }
        )

    yield step("awaiting_approval", "Awaiting your approval", "done")
    yield phase("generate_code", "active")
    yield step("generate_code", "Generating tool code", "active")
    yield blog("Generating tool code…")
    if await cancelled():
        for event in cancelled_events(run_id, "generate_code", model=creator_model):
            yield event
        return

    edit_context = plan_data.get("edit_context") if plan_data.get("kind") == "edit" else None

    accumulated = ""
    try:
        async for kind, delta in generate_tool_code_stream(
            plan_data["plan"],
            tool_name,
            creator_model,
            litellm_url=litellm_url,
            headers=litellm_headers,
            run_id=run_id,
            edit_context=edit_context if plan_data.get("kind") == "edit" else None,
            reasoning_effort=reasoning_effort,
        ):
            if await cancelled():
                for event in cancelled_events(run_id, "generate_code", model=creator_model):
                    yield event
                return
            if kind == "reasoning":
                event_name = (
                    "forge_batch_code_thinking_delta"
                    if batch_id
                    else "tool_code_thinking_delta"
                )
                yield emit({"ada_event": event_name, "run_id": run_id, "delta": delta})
                continue
            accumulated += delta
            event_name = "forge_batch_code_delta" if batch_id else "tool_code_delta"
            yield emit({"ada_event": event_name, "run_id": run_id, "delta": delta})

        tool_code = ""
        test_code = ""
        requirements: list[str] = []
        manifest: dict | None = None
        ui_files: dict[str, str] | None = None
        parse_error: Exception | None = None
        for parse_attempt in range(PHASE_MAX_RETRIES):
            try:
                if parse_attempt == 0:
                    (
                        tool_code,
                        test_code,
                        requirements,
                        manifest,
                        ui_files,
                    ) = parse_generated_tool_response(accumulated)
                break
            except Exception as exc:
                parse_error = exc
                if parse_attempt < PHASE_MAX_RETRIES - 1:
                    yield blog(f"Codegen JSON invalid ({exc}) — auto-repairing…", level="warn")
                    (
                        tool_code,
                        test_code,
                        requirements,
                        manifest,
                        ui_files,
                    ) = await repair_generated_tool_response(
                        plan_data["plan"],
                        tool_name,
                        accumulated,
                        str(exc),
                        creator_model,
                        litellm_url=litellm_url,
                        headers=litellm_headers,
                        run_id=run_id,
                        edit_context=edit_context,
                        reasoning_effort=reasoning_effort,
                    )
                    break
                raise parse_error from exc

        if manifest:
            manifest_ok, manifest_reason = validate_manifest(manifest, tool_name)
            if not manifest_ok:
                raise ValueError(f"Invalid manifest: {manifest_reason}")
            ui_ok, ui_reason = validate_ui_files(ui_files, manifest, tool_name)
            if not ui_ok:
                raise ValueError(f"Invalid ui_files: {ui_reason}")

        log_generated_code(run_id, tool_name=tool_name, tool_code=tool_code, test_code=test_code)
        yield emit(
            {
                "ada_event": "tool_code_ready",
                "run_id": run_id,
                "tool_code": tool_code,
                "test_code": test_code,
                "requirements": requirements,
                "manifest": manifest,
            }
        )
        yield blog("Code generated successfully.")
    except Exception as exc:
        log_build_event(run_id, phase="generate_code", message=str(exc), level="error")
        yield step("generate_code", "Generating tool code", "error", detail=str(exc))
        yield phase("generate_code", "error", detail=str(exc))
        yield blog(str(exc), level="error")
        yield emit(
            {
                "ada_event": "tool_build_failed",
                "run_id": run_id,
                "reason": str(exc),
            }
        )
        if on_failed:
            on_failed(plan_id, tool_name, str(exc))
        yield "data: [DONE]\n\n"
        return

    yield step("generate_code", "Generating tool code", "done")
    yield phase("generate_code", "done")

    if await cancelled():
        for event in cancelled_events(run_id, "validate_code", model=creator_model):
            yield event
        return

    yield step("validate_code", "Validating module structure", "active")
    yield phase("validate_code", "active")
    yield blog("Validating module structure…")

    validation_failed = False
    validation_reason = ""
    for val_attempt in range(PHASE_MAX_RETRIES):
        schema_ok, schema_reason = validate_tool_schema(tool_code)
        test_ok, test_reason = validate_test_code(test_code)
        if schema_ok and test_ok:
            validation_failed = False
            break

        errors: list[str] = []
        if not schema_ok:
            errors.append(schema_reason)
        if not test_ok:
            errors.append(test_reason)
        validation_reason = "; ".join(errors)

        if val_attempt < PHASE_MAX_RETRIES - 1:
            yield blog(f"Validation failed — auto-fixing ({validation_reason})…", level="warn")
            try:
                tool_code, test_code = await fix_validation_errors(
                    tool_name,
                    tool_code,
                    test_code,
                    validation_reason,
                    creator_model,
                    litellm_url=litellm_url,
                    headers=litellm_headers,
                    run_id=run_id,
                    reasoning_effort=reasoning_effort,
                )
                yield emit(
                    {
                        "ada_event": "tool_code_ready",
                        "run_id": run_id,
                        "tool_code": tool_code,
                        "test_code": test_code,
                        "requirements": requirements,
                    }
                )
                continue
            except Exception as fix_exc:
                validation_reason = str(fix_exc)
                validation_failed = True
                break
        validation_failed = True
        break

    if validation_failed:
        log_build_event(run_id, phase="validate_code", message=validation_reason, level="error")
        yield step("validate_code", "Validating module structure", "error", detail=validation_reason)
        yield phase("validate_code", "error", detail=validation_reason)
        yield blog(validation_reason, level="error")
        yield emit(
            {
                "ada_event": "tool_build_failed",
                "run_id": run_id,
                "reason": validation_reason,
            }
        )
        if on_failed:
            on_failed(plan_id, tool_name, validation_reason)
        yield "data: [DONE]\n\n"
        return

    yield step("validate_code", "Validating module structure", "done")
    yield phase("validate_code", "done")
    yield blog("Module structure and test_code look valid.")

    if await cancelled():
        for event in cancelled_events(run_id, "sandbox_test", model=creator_model):
            yield event
        return

    yield step("sandbox_test", "Running verification tests", "active")
    yield phase("sandbox_test", "active")
    yield blog("Running verification tests in isolated venv…")

    def tagged_sse(payload: dict) -> str:
        return emit(payload)

    sandbox_success, log_output, test_code, tool_code, requirements, sandbox_notices = await run_sandbox_phase(
        run_id=run_id,
        tool_name=tool_name,
        tool_code=tool_code,
        test_code=test_code,
        requirements=requirements,
        manifest=manifest,
        creator_model=creator_model,
        litellm_url=litellm_url,
        headers=litellm_headers,
        step=step,
        phase=phase,
        blog=blog,
        sse_data=tagged_sse,
        cancelled=cancelled,
        reasoning_effort=reasoning_effort,
    )
    for level, message in sandbox_notices:
        yield blog(message, level=level)
    log_sandbox(
        run_id,
        tool_name=tool_name,
        success=sandbox_success,
        logs=log_output,
        attempt=PHASE_MAX_RETRIES - 1 if not sandbox_success else 0,
    )

    if not sandbox_success:
        yield step("sandbox_test", "Running verification tests", "error", detail=log_output[:500])
        yield phase("sandbox_test", "error", detail=log_output[:200])
        yield blog(log_output, level="error")
        yield emit(
            {
                "ada_event": "tool_build_failed",
                "run_id": run_id,
                "reason": "Verification tests failed.",
                "logs": log_output,
            }
        )
        if on_failed:
            on_failed(plan_id, tool_name, "Verification tests failed.")
        yield "data: [DONE]\n\n"
        return

    yield step("sandbox_test", "Running verification tests", "done")
    yield phase("sandbox_test", "done")
    yield blog("Verification tests passed.")

    qa_passed = False
    async for event, done, new_tool, new_test, new_requirements, new_manifest, new_ui in stream_interactive_ui_qa(
        run_id=run_id,
        tool_name=tool_name,
        tool_code=tool_code,
        test_code=test_code,
        requirements=requirements,
        manifest=manifest,
        ui_files=ui_files,
        creator_model=creator_model,
        litellm_url=litellm_url,
        headers=litellm_headers,
        reasoning_effort=reasoning_effort,
        step=step,
        phase=phase,
        blog=blog,
        cancelled=cancelled,
    ):
        if event:
            if isinstance(event, str):
                yield event
            else:
                yield emit(event) if isinstance(event, dict) else event
        if done:
            qa_passed = True
            tool_code = new_tool
            test_code = new_test
            requirements = new_requirements
            manifest = new_manifest
            ui_files = new_ui
            break

    if not qa_passed:
        yield emit(
            {
                "ada_event": "tool_build_failed",
                "run_id": run_id,
                "reason": "Interactive app QA failed.",
            }
        )
        if on_failed:
            on_failed(plan_id, tool_name, "Interactive app QA failed.")
        yield "data: [DONE]\n\n"
        return

    preview_paused, preview_events = await maybe_pause_for_ui_preview(
        run_id=run_id,
        plan_id=plan_id,
        tool_name=tool_name,
        tool_code=tool_code,
        test_code=test_code,
        requirements=requirements,
        manifest=manifest,
        ui_files=ui_files,
        creator_model=creator_model,
        step=step,
        phase=phase,
        sse_data=tagged_sse,
        blog=blog,
        reasoning_effort=reasoning_effort,
        batch_id=batch_id,
    )
    if preview_paused and preview_events:
        async for event in preview_events:
            if isinstance(event, str) and event.startswith("data:"):
                yield event
            elif isinstance(event, dict):
                yield emit(event)
            else:
                yield event
        return

    async for event in continue_tool_build(
        run_id=run_id,
        plan_id=plan_id,
        tool_name=tool_name,
        tool_code=tool_code,
        test_code=test_code,
        requirements=requirements,
        manifest=manifest,
        ui_files=ui_files,
        creator_model=creator_model,
        litellm_url=litellm_url,
        litellm_headers=litellm_headers,
        step=step,
        phase=phase,
        blog=blog,
        sse_data=tagged_sse,
        cancelled=cancelled,
        reasoning_effort=reasoning_effort,
        preview_already_installed=False,
        install_lock=install_lock,
        batch_id=batch_id,
    ):
        if isinstance(event, str) and event.startswith("data:"):
            yield event
        elif isinstance(event, dict):
            yield emit(event)
        else:
            yield event

    if on_installed:
        on_installed(plan_id, tool_name, f"Tool '{tool_name}' installed.")

    if not skip_plan_cleanup and plan_id in pending_plans:
        del pending_plans[plan_id]

    if batch_id:
        yield emit(
            {
                "ada_event": "forge_batch_build_done",
                "run_id": run_id,
                "status": "installed",
            }
        )

    clear_run_cancelled(run_id)
