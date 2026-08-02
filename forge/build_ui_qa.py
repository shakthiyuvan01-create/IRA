"""Interactive skill UI QA between sandbox pass and human preview (ported from Ada-SI)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from forge.build_pipeline import PHASE_MAX_RETRIES
from forge.tool_creator import fix_preview_issues, review_interactive_preview
from forge.tool_verify import verify_skill_api_contract_in_ephemeral_venv
from forge.tools_engine import validate_ui_files

StepFn = Callable[..., str]
BlogFn = Callable[..., str]
CancelledFn = Callable[[], Awaitable[bool]]

YieldItem = tuple[str, bool, str, str, list[str], dict | None, dict[str, str] | None]


async def stream_interactive_ui_qa(
    *,
    run_id: str,
    tool_name: str,
    tool_code: str,
    test_code: str,
    requirements: list[str],
    manifest: dict | None,
    ui_files: dict[str, str] | None,
    creator_model: str,
    litellm_url: str,
    headers: dict[str, str],
    reasoning_effort: str | None,
    step: StepFn,
    phase: StepFn,
    blog: BlogFn,
    cancelled: CancelledFn,
) -> AsyncIterator[YieldItem]:
    """Yield (sse_event, done, tool_code, test_code, requirements, manifest, ui_files)."""
    if not manifest or manifest.get("kind") != "interactive":
        yield ("", True, tool_code, test_code, requirements, manifest, ui_files)
        return

    current_tool = tool_code
    current_test = test_code
    current_manifest = manifest
    current_ui = ui_files
    current_requirements = list(requirements)

    ui_fix_attempts = 0
    contract_fix_attempts = 0
    review_fix_attempts = 0

    def state(
        event: str = "",
        done: bool = False,
    ) -> YieldItem:
        return (
            event,
            done,
            current_tool,
            current_test,
            current_requirements,
            current_manifest,
            current_ui,
        )

    while True:
        if await cancelled():
            yield state(done=False)
            return

        yield state(step("validate_ui", "Validating app UI", "active"))
        yield state(phase("validate_ui", "active"))
        yield state(blog("Validating interactive UI files and SDK usage…"))

        ui_ok, ui_reason = validate_ui_files(current_ui, current_manifest, tool_name)
        if not ui_ok:
            if ui_fix_attempts < PHASE_MAX_RETRIES:
                ui_fix_attempts += 1
                yield state(
                    blog(
                        f"UI validation failed — auto-fixing (attempt {ui_fix_attempts}): {ui_reason}",
                        level="warn",
                    )
                )
                try:
                    (
                        current_tool,
                        current_test,
                        current_manifest,
                        current_ui,
                    ) = await fix_preview_issues(
                        tool_name,
                        current_tool,
                        current_test,
                        current_manifest,
                        current_ui,
                        [ui_reason],
                        creator_model,
                        litellm_url=litellm_url,
                        headers=headers,
                        run_id=run_id,
                        reasoning_effort=reasoning_effort,
                    )
                    continue
                except Exception as exc:
                    yield state(step("validate_ui", "Validating app UI", "error", detail=str(exc)[:200]))
                    yield state(done=False)
                    return
            yield state(step("validate_ui", "Validating app UI", "error", detail=ui_reason[:200]))
            yield state(done=False)
            return

        yield state(step("validate_ui", "Validating app UI", "done"))
        yield state(phase("validate_ui", "done"))

        yield state(step("contract_test", "Testing skill API contract", "active"))
        yield state(phase("contract_test", "active"))
        yield state(blog("Running API contract tests for interactive actions…"))

        contract_ok, contract_reason, current_requirements = (
            verify_skill_api_contract_in_ephemeral_venv(
                tool_name,
                current_tool,
                current_manifest,
                current_requirements,
            )
        )
        if not contract_ok:
            if contract_fix_attempts < PHASE_MAX_RETRIES:
                contract_fix_attempts += 1
                yield state(
                    blog(
                        f"Contract test failed — auto-fixing (attempt {contract_fix_attempts}): {contract_reason}",
                        level="warn",
                    )
                )
                try:
                    (
                        current_tool,
                        current_test,
                        current_manifest,
                        current_ui,
                    ) = await fix_preview_issues(
                        tool_name,
                        current_tool,
                        current_test,
                        current_manifest,
                        current_ui,
                        [contract_reason],
                        creator_model,
                        litellm_url=litellm_url,
                        headers=headers,
                        run_id=run_id,
                        reasoning_effort=reasoning_effort,
                    )
                    continue
                except Exception as exc:
                    yield state(
                        step(
                            "contract_test",
                            "Testing skill API contract",
                            "error",
                            detail=str(exc)[:200],
                        )
                    )
                    yield state(done=False)
                    return
            yield state(
                step(
                    "contract_test",
                    "Testing skill API contract",
                    "error",
                    detail=contract_reason[:200],
                )
            )
            yield state(done=False)
            return

        yield state(step("contract_test", "Testing skill API contract", "done"))
        yield state(phase("contract_test", "done"))

        yield state(step("preview_review", "Automated app review", "active"))
        yield state(phase("preview_review", "active"))
        yield state(blog("Running automated preview review…"))

        review_ok, review_issues = await review_interactive_preview(
            tool_name,
            current_tool,
            current_test,
            current_manifest,
            current_ui,
            creator_model,
            litellm_url=litellm_url,
            headers=headers,
            run_id=run_id,
            reasoning_effort=reasoning_effort,
        )
        if not review_ok:
            if review_fix_attempts < PHASE_MAX_RETRIES:
                review_fix_attempts += 1
                summary = "; ".join(review_issues) or "Preview review failed."
                yield state(
                    blog(
                        f"Preview review flagged issues — auto-fixing (attempt {review_fix_attempts}): {summary}",
                        level="warn",
                    )
                )
                try:
                    (
                        current_tool,
                        current_test,
                        current_manifest,
                        current_ui,
                    ) = await fix_preview_issues(
                        tool_name,
                        current_tool,
                        current_test,
                        current_manifest,
                        current_ui,
                        review_issues or ["Preview review failed."],
                        creator_model,
                        litellm_url=litellm_url,
                        headers=headers,
                        run_id=run_id,
                        reasoning_effort=reasoning_effort,
                    )
                    continue
                except Exception as exc:
                    yield state(
                        step("preview_review", "Automated app review", "error", detail=str(exc)[:200])
                    )
                    yield state(done=False)
                    return
            summary = "; ".join(review_issues) or "Preview review failed."
            yield state(step("preview_review", "Automated app review", "error", detail=summary[:200]))
            yield state(done=False)
            return

        yield state(step("preview_review", "Automated app review", "done"))
        yield state(phase("preview_review", "done"))
        yield state(blog("Automated preview review passed."))
        yield state(done=True)
        return
