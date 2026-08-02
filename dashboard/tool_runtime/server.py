"""IRA Tool Runtime HTTP API — FastAPI server for sandboxed tool execution."""

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from dashboard.tool_runtime.runner import (
    delete_tool,
    install_tool,
    list_installed_packages,
    list_tools,
    load_manifest,
    pip_install,
    pip_uninstall,
    run_tool,
    verify_tool_in_runtime,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="IRA Tool Runtime")


class RunRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class InstallRequest(BaseModel):
    tool_code: str
    test_code: str
    requirements: list[str] = Field(default_factory=list)
    skip_pip: bool = False


class PipInstallRequest(BaseModel):
    packages: list[str]


class VerifyRequest(BaseModel):
    test_code: str


@app.get("/health")
async def health() -> dict:
    manifest = load_manifest()
    return {"status": "ok", "approved_packages": manifest.get("approved_packages", [])}


@app.get("/tools")
async def get_tools() -> dict:
    tools = list_tools()
    return {"tools": [{"name": t["name"], "description": t["description"], "schema": t["schema"]} for t in tools]}


@app.post("/tools/{name}/run")
async def execute_tool(name: str, payload: RunRequest) -> dict:
    try:
        result = run_tool(name, payload.arguments)
        return {"status": "ok", "result": result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Tool run failed: %s", name)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/tools/{name}/install")
async def install(name: str, payload: InstallRequest) -> dict:
    ok, logs = install_tool(name, payload.tool_code, payload.test_code, payload.requirements, skip_pip=payload.skip_pip)
    if not ok:
        logger.error("Install failed for %s:\n%s", name, logs)
        raise HTTPException(status_code=502, detail=logs)
    return {"status": "ok", "logs": logs}


@app.post("/tools/{name}/verify")
async def verify(name: str, payload: VerifyRequest) -> dict:
    if not (payload.test_code or "").strip():
        raise HTTPException(status_code=400, detail="test_code is required.")
    ok, logs = verify_tool_in_runtime(name, payload.test_code)
    if not ok:
        raise HTTPException(status_code=502, detail=logs)
    return {"status": "ok", "logs": logs}


@app.post("/pip/install")
async def pip_install_endpoint(payload: PipInstallRequest) -> dict:
    packages = [p.strip() for p in payload.packages if p.strip()]
    if not packages:
        raise HTTPException(status_code=400, detail="No packages specified.")
    ok, logs = pip_install(packages)
    if not ok:
        raise HTTPException(status_code=502, detail=logs)
    return {"status": "ok", "logs": logs}


@app.get("/pip/packages")
async def list_pip_packages() -> dict:
    return {"packages": list_installed_packages()}


@app.delete("/pip/packages/{package_name}")
async def uninstall_pip_package(package_name: str) -> dict:
    ok, logs = pip_uninstall(package_name)
    if not ok:
        if "not in the approved manifest" in logs:
            raise HTTPException(status_code=404, detail=logs)
        raise HTTPException(status_code=502, detail=logs)
    return {"status": "ok", "logs": logs, "packages": list_installed_packages()}


@app.get("/manifest")
async def manifest() -> dict:
    return load_manifest()


@app.delete("/tools/{name}")
async def remove_tool(name: str) -> dict:
    delete_tool(name)
    return {"status": "deleted", "tool_name": name}
