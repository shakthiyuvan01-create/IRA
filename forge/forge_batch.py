"""Multi-tool forge batch orchestration."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from forge.tool_creator import draft_tool_plan_stream, revise_tool_plan_stream

BATCH_TTL_SECONDS = 3600
BATCH_MAX_TOOLS = 10
BATCH_MIN_TOOLS = 2

PENDING_FORGE_BATCHES: dict[str, dict] = {}
RUNTIME_INSTALL_LOCK = asyncio.Lock()

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


def cleanup_expired_batches() -> None:
    now = time.time()
    expired = [
        batch_id
        for batch_id, data in PENDING_FORGE_BATCHES.items()
        if now - data.get("created_at", now) > BATCH_TTL_SECONDS
    ]
    for batch_id in expired:
        del PENDING_FORGE_BATCHES[batch_id]


def get_pending_batch(batch_id: str) -> dict:
    cleanup_expired_batches()
    batch = PENDING_FORGE_BATCHES.get(batch_id)
    if batch is None:
        raise ValueError("Forge batch not found or expired.")
    return batch


def validate_batch_tools(
    tools: list[dict],
    *,
    tool_exists: Callable[[str], bool],
) -> list[dict]:
    if not isinstance(tools, list):
        raise ValueError("tools must be an array.")
    if len(tools) < BATCH_MIN_TOOLS or len(tools) > BATCH_MAX_TOOLS:
        raise ValueError(f"Batch must contain {BATCH_MIN_TOOLS}–{BATCH_MAX_TOOLS} tools.")

    seen: set[str] = set()
    normalized: list[dict] = []
    for item in tools:
        if not isinstance(item, dict):
            raise ValueError("Each tool entry must be an object.")
        tool_name = str(item.get("tool_name", "")).strip()
        description = str(item.get("description", "")).strip()
        if not tool_name or not description:
            raise ValueError("Each tool requires tool_name and description.")
        if not _SNAKE_CASE.match(tool_name):
            raise ValueError(f"Invalid tool_name '{tool_name}' (use snake_case).")
        if tool_name in seen:
            raise ValueError(f"Duplicate tool_name '{tool_name}' in batch.")
        if tool_exists(tool_name):
            raise ValueError(f"Tool '{tool_name}' is already installed.")
        seen.add(tool_name)
        normalized.append({"tool_name": tool_name, "description": description})
    return normalized


def create_batch(
    *,
    run_id: str,
    tools: list[dict],
    summary: str,
    creator_model: str,
    reasoning_effort: str | None,
) -> tuple[str, dict]:
    batch_id = uuid.uuid4().hex
    entries: list[dict] = []
    for index, tool in enumerate(tools):
        plan_id = uuid.uuid4().hex
        entries.append(
            {
                "plan_id": plan_id,
                "tool_name": tool["tool_name"],
                "description": tool["description"],
                "batch_index": index,
                "status": "queued",
            }
        )

    batch = {
        "batch_id": batch_id,
        "run_id": run_id,
        "summary": summary,
        "creator_model": creator_model,
        "reasoning_effort": reasoning_effort,
        "tools": entries,
        "status": "proposed",
        "created_at": time.time(),
        "results": {},
    }
    PENDING_FORGE_BATCHES[batch_id] = batch
    return batch_id, batch


def batch_sse(
    payload: dict,
    *,
    batch_id: str,
    plan_id: str | None = None,
    tool_name: str | None = None,
) -> str:
    data = {**payload, "batch_id": batch_id}
    if plan_id:
        data["plan_id"] = plan_id
    if tool_name:
        data["tool_name"] = tool_name
    return f"data: {json.dumps(data)}\n\n"


def _entry_by_plan_id(batch: dict, plan_id: str) -> dict:
    for entry in batch["tools"]:
        if entry["plan_id"] == plan_id:
            return entry
    raise ValueError(f"plan_id '{plan_id}' not in batch.")


def set_entry_status(batch: dict, plan_id: str, status: str) -> None:
    entry = _entry_by_plan_id(batch, plan_id)
    entry["status"] = status


def all_entries_at_least(batch: dict, min_status: str) -> bool:
    order = [
        "queued",
        "drafting",
        "plan_ready",
        "plan_approved",
        "building",
        "pip_pending",
        "ui_preview_pending",
        "done",
        "failed",
        "skipped",
    ]

    def rank(status: str) -> int:
        try:
            return order.index(status)
        except ValueError:
            return -1

    min_rank = rank(min_status)
    for entry in batch["tools"]:
        if entry["status"] in ("skipped", "failed"):
            continue
        if rank(entry["status"]) < min_rank:
            return False
    return True


def all_plans_drafted(batch: dict) -> bool:
    for entry in batch["tools"]:
        if entry["status"] in ("skipped", "failed"):
            continue
        if entry["status"] in ("queued", "drafting"):
            return False
    return True


def approve_plan(batch_id: str, plan_id: str) -> dict:
    batch = get_pending_batch(batch_id)
    entry = _entry_by_plan_id(batch, plan_id)
    if entry["status"] == "plan_ready":
        entry["status"] = "plan_approved"
    elif entry["status"] != "plan_approved":
        raise ValueError(f"Plan '{plan_id}' is not ready for approval.")
    return batch


def approve_all_plans(batch_id: str) -> dict:
    batch = get_pending_batch(batch_id)
    if not all_plans_drafted(batch):
        raise ValueError("Not all plans have finished drafting.")
    for entry in batch["tools"]:
        if entry["status"] == "plan_ready":
            entry["status"] = "plan_approved"
    return batch


def reject_plan(batch_id: str, plan_id: str) -> dict:
    batch = get_pending_batch(batch_id)
    set_entry_status(batch, plan_id, "skipped")
    batch["results"][plan_id] = {"status": "skipped", "tool_name": _entry_by_plan_id(batch, plan_id)["tool_name"]}
    return batch


def cancel_batch(batch_id: str) -> None:
    batch = get_pending_batch(batch_id)
    batch["status"] = "cancelled"
    for entry in batch["tools"]:
        if entry["status"] not in ("done", "failed", "skipped"):
            entry["status"] = "skipped"
    del PENDING_FORGE_BATCHES[batch_id]


def plan_ids_ready_to_build(batch: dict, plan_id: str | None) -> list[str]:
    if plan_id:
        entry = _entry_by_plan_id(batch, plan_id)
        if entry["status"] != "plan_approved":
            raise ValueError(f"Plan '{plan_id}' is not approved for forging.")
        return [plan_id]
    ids = [
        e["plan_id"]
        for e in batch["tools"]
        if e["status"] == "plan_approved"
    ]
    if not ids:
        raise ValueError("No approved plans ready to forge.")
    return ids


def batch_terminal(batch: dict) -> bool:
    for entry in batch["tools"]:
        if entry["status"] not in ("done", "failed", "skipped"):
            return False
    return True


def mark_batch_tool_installed(
    batch_id: str,
    plan_id: str,
    tool_name: str,
    message: str,
) -> dict | None:
    try:
        batch = get_pending_batch(batch_id)
    except ValueError:
        return None
    set_entry_status(batch, plan_id, "done")
    batch.setdefault("results", {})[plan_id] = {
        "status": "installed",
        "tool_name": tool_name,
        "message": message,
    }
    return batch


def mark_batch_tool_failed(
    batch_id: str,
    plan_id: str,
    tool_name: str,
    reason: str,
) -> dict | None:
    try:
        batch = get_pending_batch(batch_id)
    except ValueError:
        return None
    set_entry_status(batch, plan_id, "failed")
    batch.setdefault("results", {})[plan_id] = {
        "status": "failed",
        "tool_name": tool_name,
        "reason": reason,
    }
    return batch


def build_resume_summary(batch: dict) -> str:
    lines: list[str] = []
    for entry in batch["tools"]:
        result = batch.get("results", {}).get(entry["plan_id"], {})
        status = result.get("status", entry["status"])
        name = entry["tool_name"]
        if status == "installed":
            lines.append(f"- {name}: installed successfully")
        elif status == "failed":
            lines.append(f"- {name}: failed ({result.get('reason', 'unknown')})")
        elif status == "skipped":
            lines.append(f"- {name}: skipped by user")
        else:
            lines.append(f"- {name}: {status}")
    return "Multi-tool forge batch results:\n" + "\n".join(lines)


_SCOUT_RESUME_SUFFIX = """The tool(s) above are installed and available now. Continue helping the user:
- Call the relevant tool to fulfill their original request when applicable.
- Do not call generate_new_tool or propose_tool_batch for the same capability again.
- Give a helpful reply with results or next steps."""


def build_batch_resume_message(details: str) -> str:
    return f"[System] Tool forge completed.\n\n{details.strip()}\n\n{_SCOUT_RESUME_SUFFIX}"


def build_single_tool_resume_message(tool_name: str, message: str = "") -> str:
    line = f"- {tool_name}: installed"
    if message.strip():
        line += f" ({message.strip()})"
    return build_batch_resume_message(line)


def build_batch_resume_message_from_batch(batch: dict) -> str:
    summary = build_resume_summary(batch)
    details = summary.replace("Multi-tool forge batch results:\n", "", 1)
    return build_batch_resume_message(details)


async def stream_batch_plan_draft(
    *,
    batch_id: str,
    plan_id: str,
    tool_name: str,
    description: str,
    creator_model: str,
    litellm_url: str,
    headers: dict[str, str],
    run_id: str,
    reasoning_effort: str | None,
    queue: asyncio.Queue[str | None],
    pending_plans: dict[str, dict],
) -> None:
    """Draft one plan and push batch SSE strings into queue."""
    out: dict[str, str] = {"plan": ""}
    try:
        await queue.put(
            batch_sse(
                {
                    "ada_event": "forge_batch_plan_started",
                    "run_id": run_id,
                },
                batch_id=batch_id,
                plan_id=plan_id,
                tool_name=tool_name,
            )
        )

        plan_stream = draft_tool_plan_stream(
            tool_name,
            description,
            creator_model,
            litellm_url=litellm_url,
            headers=headers,
            run_id=run_id,
            reasoning_effort=reasoning_effort,
        )
        async for chunk_kind, delta in plan_stream:
            if chunk_kind == "reasoning":
                await queue.put(
                    batch_sse(
                        {
                            "ada_event": "forge_batch_plan_thinking_delta",
                            "run_id": run_id,
                            "delta": delta,
                        },
                        batch_id=batch_id,
                        plan_id=plan_id,
                        tool_name=tool_name,
                    )
                )
            elif chunk_kind == "content":
                out["plan"] += delta
                await queue.put(
                    batch_sse(
                        {
                            "ada_event": "forge_batch_plan_content_delta",
                            "run_id": run_id,
                            "delta": delta,
                        },
                        batch_id=batch_id,
                        plan_id=plan_id,
                        tool_name=tool_name,
                    )
                )

        plan = out.get("plan", "")
        pending_plans[plan_id] = {
            "tool_name": tool_name,
            "description": description,
            "plan": plan,
            "creator_model": creator_model,
            "created_at": time.time(),
            "run_id": run_id,
            "batch_id": batch_id,
            "kind": "create",
        }
        batch = get_pending_batch(batch_id)
        set_entry_status(batch, plan_id, "plan_ready")

        await queue.put(
            batch_sse(
                {
                    "ada_event": "forge_batch_plan_ready",
                    "run_id": run_id,
                    "plan": plan,
                },
                batch_id=batch_id,
                plan_id=plan_id,
                tool_name=tool_name,
            )
        )
    except Exception as exc:
        batch = get_pending_batch(batch_id)
        set_entry_status(batch, plan_id, "failed")
        batch["results"][plan_id] = {
            "status": "failed",
            "tool_name": tool_name,
            "reason": str(exc),
        }
        await queue.put(
            batch_sse(
                {
                    "ada_event": "forge_batch_plan_failed",
                    "run_id": run_id,
                    "reason": str(exc),
                },
                batch_id=batch_id,
                plan_id=plan_id,
                tool_name=tool_name,
            )
        )


async def stream_parallel_plan_phase(
    *,
    batch_id: str,
    litellm_url: str,
    headers: dict[str, str],
    pending_plans: dict[str, dict],
    cancelled: Callable[[], Any],
) -> AsyncIterator[str]:
    """Yield SSE while drafting all batch plans in parallel."""
    batch = get_pending_batch(batch_id)
    batch["status"] = "drafting"
    run_id = batch["run_id"]
    creator_model = batch["creator_model"]
    reasoning_effort = batch.get("reasoning_effort")

    yield batch_sse(
        {"ada_event": "forge_batch_plan_phase_started", "run_id": run_id},
        batch_id=batch_id,
    )

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    tasks: list[asyncio.Task] = []

    for entry in batch["tools"]:
        if entry["status"] == "skipped":
            continue
        entry["status"] = "drafting"
        tasks.append(
            asyncio.create_task(
                stream_batch_plan_draft(
                    batch_id=batch_id,
                    plan_id=entry["plan_id"],
                    tool_name=entry["tool_name"],
                    description=entry["description"],
                    creator_model=creator_model,
                    litellm_url=litellm_url,
                    headers=headers,
                    run_id=run_id,
                    reasoning_effort=reasoning_effort,
                    queue=queue,
                    pending_plans=pending_plans,
                )
            )
        )

    async def _join_tasks() -> None:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    join_task = asyncio.create_task(_join_tasks())

    while True:
        if await cancelled():
            for task in tasks:
                task.cancel()
            join_task.cancel()
            return
        item = await queue.get()
        if item is None:
            break
        yield item

    await join_task
    batch = get_pending_batch(batch_id)
    batch["status"] = "plans_ready"
    yield batch_sse(
        {"ada_event": "forge_batch_plan_phase_done", "run_id": run_id},
        batch_id=batch_id,
    )


async def stream_batch_plan_revision(
    *,
    batch_id: str,
    plan_id: str,
    feedback: str,
    litellm_url: str,
    headers: dict[str, str],
    pending_plans: dict[str, dict],
    cancelled: Callable[[], Any],
) -> AsyncIterator[str]:
    batch = get_pending_batch(batch_id)
    plan_data = pending_plans.get(plan_id)
    if plan_data is None:
        raise ValueError("Plan not found.")

    entry = _entry_by_plan_id(batch, plan_id)
    tool_name = entry["tool_name"]
    run_id = batch["run_id"]
    creator_model = batch["creator_model"]
    reasoning_effort = batch.get("reasoning_effort")

    entry["status"] = "drafting"
    yield batch_sse(
        {"ada_event": "forge_batch_plan_started", "run_id": run_id},
        batch_id=batch_id,
        plan_id=plan_id,
        tool_name=tool_name,
    )

    out: dict[str, str] = {"plan": ""}
    async for chunk_kind, delta in revise_tool_plan_stream(
        tool_name,
        plan_data["plan"],
        feedback,
        creator_model,
        litellm_url=litellm_url,
        headers=headers,
        run_id=run_id,
        reasoning_effort=reasoning_effort,
    ):
        if await cancelled():
            return
        if chunk_kind == "reasoning":
            yield batch_sse(
                {
                    "ada_event": "forge_batch_plan_thinking_delta",
                    "run_id": run_id,
                    "delta": delta,
                },
                batch_id=batch_id,
                plan_id=plan_id,
                tool_name=tool_name,
            )
        elif chunk_kind == "content":
            out["plan"] += delta
            yield batch_sse(
                {
                    "ada_event": "forge_batch_plan_content_delta",
                    "run_id": run_id,
                    "delta": delta,
                },
                batch_id=batch_id,
                plan_id=plan_id,
                tool_name=tool_name,
            )

    plan = out.get("plan", "")
    plan_data["plan"] = plan
    entry["status"] = "plan_ready"
    yield batch_sse(
        {
            "ada_event": "forge_batch_plan_ready",
            "run_id": run_id,
            "plan": plan,
        },
        batch_id=batch_id,
        plan_id=plan_id,
        tool_name=tool_name,
    )


async def merge_async_generators(
    generators: list[AsyncIterator[str]],
) -> AsyncIterator[str]:
    """Merge multiple async generators into one stream."""
    if not generators:
        return
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def _pump(gen: AsyncIterator[str]) -> None:
        try:
            async for item in gen:
                await queue.put(item)
        finally:
            await queue.put(None)

    tasks = [asyncio.create_task(_pump(g)) for g in generators]
    done = 0
    while done < len(tasks):
        item = await queue.get()
        if item is None:
            done += 1
        else:
            yield item
    await asyncio.gather(*tasks, return_exceptions=True)
