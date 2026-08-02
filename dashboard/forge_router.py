"""IRA Forge API Router — tool creation, pip management, previews."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from forge.build_pipeline import (
    PENDING_PIP_INSTALLS,
    PENDING_UI_PREVIEWS,
    PHASE_MAX_RETRIES,
    continue_tool_build,
    get_pending_pip,
    get_pending_ui_preview,
    maybe_pause_for_pip_approval,
    maybe_pause_for_ui_preview,
    run_sandbox_phase,
    stream_runtime_install,
)
from forge.build_ui_qa import stream_interactive_ui_qa
from forge.debug_log import log_build_event
from forge.forge_batch import (
    RUNTIME_INSTALL_LOCK,
    approve_all_plans,
    approve_plan,
    cancel_batch,
    create_batch,
    get_pending_batch,
    merge_async_generators,
    plan_ids_ready_to_build,
    reject_plan,
    stream_batch_plan_revision,
    validate_batch_tools,
)
from forge.litellm_client import SSE_HEADERS
from forge.runtime_client import (
    runtime_health,
    runtime_list_pip_packages,
    runtime_uninstall_pip_package,
)
from forge.tool_build_stream import stream_tool_build
from forge.tool_creator import (
    draft_tool_plan_stream,
    revise_tool_plan_stream,
)
from forge.tools_engine import (
    alist_tool_summaries,
    delete_tool_async,
    execute_skill_action,
    get_package_usage,
    is_interactive_skill,
    read_skill_data,
    resolve_skill_ui_file,
    skill_ui_entry_path,
    tool_exists,
    ui_content_type,
    write_skill_data,
)

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent

router = APIRouter(prefix="/api")

PENDING_PLANS: dict[str, dict] = {}
RUN_CANCEL_FLAGS: set[str] = set()
PLAN_TTL_SECONDS = 3600


def _get(key: str, default: str = "") -> str:
    import os
    return (os.environ.get(key) or "").strip() or default


def cleanup_expired_plans() -> None:
    now = time.time()
    for pid in [p for p, d in PENDING_PLANS.items() if now - d.get("created_at", now) > PLAN_TTL_SECONDS]:
        PENDING_PLANS.pop(pid, None)


def get_pending_plan(plan_id: str) -> dict:
    cleanup_expired_plans()
    p = PENDING_PLANS.get(plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="Plan not found.")
    return p


def is_run_cancelled(run_id: str) -> bool:
    return bool(run_id and run_id in RUN_CANCEL_FLAGS)


def mark_run_cancelled(run_id: str) -> None:
    if run_id:
        RUN_CANCEL_FLAGS.add(run_id)


def clear_run_cancelled(run_id: str) -> None:
    RUN_CANCEL_FLAGS.discard(run_id)


def sse_data(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def pstep(run_id: str, sid: str, label: str, status: str, *, model: str = "", detail: str = "") -> str:
    return sse_data({"ada_event": "process_step", "run_id": run_id, "step_id": sid,
                     "label": label, "status": status, "model": model, "detail": detail})


def tphase(run_id: str, phase: str, status: str, *, detail: str = "") -> str:
    return sse_data({"ada_event": "tool_build_phase", "run_id": run_id, "phase": phase,
                     "status": status, "detail": detail})


def tblog(run_id: str, msg: str, *, level: str = "info") -> str:
    return sse_data({"ada_event": "tool_build_log", "run_id": run_id, "level": level, "message": msg})


def cancelled_events(run_id: str, sid: str, *, model: str = "") -> list[str]:
    return [pstep(run_id, sid, "Stopped by user", "error", model=model),
            sse_data({"ada_event": "run_cancelled", "run_id": run_id}),
            "data: [DONE]\n\n"]


async def stream_plan_events(run_id: str, tool_name: str, stream, *, kind="create", plan_id="", out=None):
    if out is None:
        out = {}
    out["plan"] = ""
    yield sse_data({"ada_event": "tool_plan_draft_started", "run_id": run_id, "tool_name": tool_name, "kind": kind, "plan_id": plan_id})
    async for ck, delta in stream:
        if ck == "reasoning":
            yield sse_data({"ada_event": "tool_plan_thinking_delta", "run_id": run_id, "delta": delta})
        elif ck == "content":
            out["plan"] += delta
            yield sse_data({"ada_event": "tool_plan_content_delta", "run_id": run_id, "delta": delta})


# ── Tool Plan ────────────────────────────────────────────────────────────

@router.post("/forge/plan")
async def forge_draft_plan(payload: dict = Body(...)):
    tool_name = str(payload.get("tool_name", "")).strip()
    desc = str(payload.get("description", "")).strip()
    if not tool_name or not desc:
        raise HTTPException(status_code=400, detail="tool_name and description required.")
    model = _get("TOOL_CREATOR_MODEL", "gpt-4o")
    run_id = payload.get("run_id") or uuid.uuid4().hex

    async def gen():
        yield pstep(run_id, "plan_draft", "Drafting tool plan", "active", model=model)
        po: dict = {}
        async for ev in stream_plan_events(run_id, tool_name,
            draft_tool_plan_stream(tool_name, desc, model, litellm_url=_get("LITELLM_URL", "http://localhost:4000"),
                                   headers={"Content-Type": "application/json"}, run_id=run_id), out=po):
            yield ev
        plan_id = uuid.uuid4().hex
        PENDING_PLANS[plan_id] = {"tool_name": tool_name, "description": desc, "plan": po.get("plan", ""),
                                   "creator_model": model, "created_at": time.time(), "run_id": run_id}
        yield sse_data({"ada_event": "tool_plan_pending", "run_id": run_id, "plan_id": plan_id,
                        "tool_name": tool_name, "plan": po.get("plan", ""), "kind": "create"})
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/forge/approve")
async def forge_approve(request: Request, payload: dict = Body(...)):
    plan_id = payload.get("plan_id", "").strip()
    if not plan_id:
        raise HTTPException(status_code=400, detail="plan_id required.")
    pd = get_pending_plan(plan_id)
    run_id = pd.get("run_id", "")
    lurl = _get("LITELLM_URL", "http://localhost:4000")
    lh = {"Content-Type": "application/json"}
    async def gen():
        async for ev in stream_tool_build(plan_id=plan_id, plan_data=pd, run_id=run_id,
            creator_model=pd["creator_model"], reasoning_effort=None,
            litellm_url=lurl, litellm_headers=lh, pending_plans=PENDING_PLANS,
            process_step=pstep, tool_build_phase=tphase, tool_build_log=tblog,
            sse_data=sse_data, cancelled_events=cancelled_events,
            is_run_cancelled=is_run_cancelled, clear_run_cancelled=clear_run_cancelled,
            cancelled_override=lambda: is_run_cancelled(run_id)):
            yield ev
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/forge/revise")
async def forge_revise(request: Request, payload: dict = Body(...)):
    plan_id = payload.get("plan_id", "").strip()
    feedback = payload.get("feedback", "").strip()
    if not plan_id or not feedback:
        raise HTTPException(status_code=400, detail="plan_id and feedback required.")
    pd = get_pending_plan(plan_id)
    run_id = pd.get("run_id", "")
    async def gen():
        po: dict = {}
        async for ev in stream_plan_events(run_id, pd["tool_name"],
            revise_tool_plan_stream(pd["tool_name"], pd.get("description", ""), pd["plan"], feedback,
                                    pd["creator_model"], litellm_url=_get("LITELLM_URL", "http://localhost:4000"),
                                    headers={"Content-Type": "application/json"}, run_id=run_id),
            kind=pd.get("kind", "create"), plan_id=plan_id, out=po):
            yield ev
        pd["plan"] = po.get("plan", "")
        pd["created_at"] = time.time()
        yield sse_data({"ada_event": "tool_plan_revised", "run_id": run_id, "plan_id": plan_id,
                        "tool_name": pd["tool_name"], "plan": pd["plan"]})
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/forge/reject")
async def forge_reject(payload: dict = Body(...)):
    pid = payload.get("plan_id", "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="plan_id required.")
    PENDING_PLANS.pop(pid, None)
    return {"status": "rejected"}


@router.post("/forge/cancel_run")
async def forge_cancel_run(payload: dict = Body(...)):
    rid = payload.get("run_id", "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="run_id required.")
    mark_run_cancelled(rid)
    return {"status": "cancelled"}


# ── Tools ────────────────────────────────────────────────────────────────

@router.get("/forge/tools")
async def forge_list_tools():
    return {"tools": await alist_tool_summaries()}


@router.delete("/forge/tools/{tool_name}")
async def forge_delete_tool(tool_name: str):
    try:
        await delete_tool_async(tool_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "tool_name": tool_name}


# ── Pip ──────────────────────────────────────────────────────────────────

@router.get("/forge/pip/packages")
async def forge_pip_list():
    try:
        pkgs = await runtime_list_pip_packages()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    usage = get_package_usage()
    return {"packages": [{**p, "used_by": usage.get((p.get("name") or "").lower(), [])} for p in pkgs]}


@router.delete("/forge/pip/packages/{name}")
async def forge_pip_uninstall(name: str):
    n = name.strip().lower()
    if not n:
        raise HTTPException(status_code=400, detail="Package name required.")
    deps = get_package_usage().get(n, [])
    if deps:
        raise HTTPException(status_code=409, detail={"message": f"Package '{n}' required by installed tools.", "used_by": deps})
    pkgs = await runtime_uninstall_pip_package(n)
    return {"status": "deleted", "packages": pkgs}


# ── Batch Forge ──────────────────────────────────────────────────────────

@router.post("/forge/batch/create")
async def forge_batch_create(payload: dict = Body(...)):
    tools_raw = payload.get("tools", [])
    summary = str(payload.get("summary", "")).strip()
    if not summary:
        raise HTTPException(status_code=400, detail="summary required.")
    try:
        tools = validate_batch_tools(tools_raw, tool_exists=tool_exists)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    bid, batch = create_batch(run_id=payload.get("run_id", "") or uuid.uuid4().hex, tools=tools, summary=summary,
                               creator_model=_get("TOOL_CREATOR_MODEL", "gpt-4o"), reasoning_effort=None)
    return {"batch_id": bid, "tools": [{"tool_name": t["tool_name"], "plan_id": t["plan_id"]} for t in batch["tools"]]}


@router.post("/forge/batch/approve_plan")
async def forge_batch_approve_plan(payload: dict = Body(...)):
    bid = payload.get("batch_id", "").strip()
    pid = payload.get("plan_id", "").strip()
    if not bid or not pid:
        raise HTTPException(status_code=400, detail="batch_id and plan_id required.")
    approve_plan(bid, pid)
    return {"status": "approved"}


@router.post("/forge/batch/approve_all")
async def forge_batch_approve_all(payload: dict = Body(...)):
    bid = payload.get("batch_id", "").strip()
    if not bid:
        raise HTTPException(status_code=400, detail="batch_id required.")
    approve_all_plans(bid)
    return {"status": "approved_all"}


@router.post("/forge/batch/reject_plan")
async def forge_batch_reject_plan(payload: dict = Body(...)):
    bid = payload.get("batch_id", "").strip()
    pid = payload.get("plan_id", "").strip()
    if not bid or not pid:
        raise HTTPException(status_code=400, detail="batch_id and plan_id required.")
    reject_plan(bid, pid)
    return {"status": "skipped"}


@router.post("/forge/batch/cancel")
async def forge_batch_cancel(payload: dict = Body(...)):
    bid = payload.get("batch_id", "").strip()
    if not bid:
        raise HTTPException(status_code=400, detail="batch_id required.")
    cancel_batch(bid)
    return {"status": "cancelled"}


@router.post("/forge/batch/revise_plan")
async def forge_batch_revise_plan(request: Request, payload: dict = Body(...)):
    bid = payload.get("batch_id", "").strip()
    pid = payload.get("plan_id", "").strip()
    fb = payload.get("feedback", "").strip()
    if not bid or not pid or not fb:
        raise HTTPException(status_code=400, detail="batch_id, plan_id, and feedback required.")
    async def gen():
        async for ev in stream_batch_plan_revision(batch_id=bid, plan_id=pid, feedback=fb,
            litellm_url=_get("LITELLM_URL", "http://localhost:4000"), headers={"Content-Type": "application/json"},
            pending_plans=PENDING_PLANS, cancelled=lambda: False):
            yield ev
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/forge/batch/start_build")
async def forge_batch_start_build(request: Request, payload: dict = Body(...)):
    bid = payload.get("batch_id", "").strip()
    pid = payload.get("plan_id", "").strip() or None
    if not bid:
        raise HTTPException(status_code=400, detail="batch_id required.")
    try:
        batch = get_pending_batch(bid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        ids = plan_ids_ready_to_build(batch, pid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run_id = batch["run_id"]
    lurl = _get("LITELLM_URL", "http://localhost:4000")
    lh = {"Content-Type": "application/json"}
    async def gen():
        async for ev in merge_async_generators([
            stream_tool_build(plan_id=i, plan_data=get_pending_plan(i), run_id=run_id,
                creator_model=batch["creator_model"], reasoning_effort=batch.get("reasoning_effort"),
                litellm_url=lurl, litellm_headers=lh, pending_plans=PENDING_PLANS,
                process_step=pstep, tool_build_phase=tphase, tool_build_log=tblog,
                sse_data=sse_data, cancelled_events=cancelled_events,
                is_run_cancelled=is_run_cancelled, clear_run_cancelled=clear_run_cancelled,
                cancelled_override=lambda: is_run_cancelled(run_id),
                batch_id=bid, install_lock=RUNTIME_INSTALL_LOCK) for i in ids]):
            yield ev
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# ── Skill UI ─────────────────────────────────────────────────────────────

CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"


@router.get("/forge/skills/{name}/ui")
async def forge_skill_ui(name: str):
    if not tool_exists(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")
    if not is_interactive_skill(name):
        raise HTTPException(status_code=400, detail="Not an interactive skill.")
    entry = skill_ui_entry_path(name)
    if not entry:
        raise HTTPException(status_code=404, detail="UI entry not found.")
    return FileResponse(str(entry), media_type=ui_content_type(entry), headers={"Content-Security-Policy": CSP})


@router.get("/forge/skills/{name}/ui/{path:path}")
async def forge_skill_ui_file(name: str, path: str):
    if not tool_exists(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")
    entry = resolve_skill_ui_file(name, path)
    if not entry:
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(str(entry), media_type=ui_content_type(entry), headers={"Content-Security-Policy": CSP})


@router.post("/forge/skills/{name}/action")
async def forge_skill_action(name: str, payload: dict = Body(...)):
    if not tool_exists(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")
    try:
        return await execute_skill_action(name, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/forge/skills/{name}/data")
async def forge_skill_data(name: str):
    if not tool_exists(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")
    return read_skill_data(name)


@router.put("/forge/skills/{name}/data")
async def forge_skill_data_put(name: str, payload: dict = Body(...)):
    if not tool_exists(name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")
    if not is_interactive_skill(name):
        raise HTTPException(status_code=400, detail="Not an interactive skill.")
    write_skill_data(name, payload)
    return {"ok": True}


# ── Health ───────────────────────────────────────────────────────────────

@router.get("/forge/health")
async def forge_health():
    ok, reason = await runtime_health()
    return {"status": "ok", "tool_runtime_available": ok, "tool_runtime_reason": reason}
