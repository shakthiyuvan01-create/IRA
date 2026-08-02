"""Tool runtime HTTP client for IRA Forge.

Communicates with the Docker-based tool runtime sandbox at port 8090."""

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOOL_RUNTIME_URL = "http://localhost:8090"


def set_runtime_url(url: str) -> None:
    global TOOL_RUNTIME_URL
    TOOL_RUNTIME_URL = url.rstrip("/")


def package_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[]", requirement.strip())[0].strip().lower()


def normalize_requirements(requirements: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for req in requirements:
        req = str(req).strip()
        if not req or req in seen:
            continue
        seen.add(req)
        out.append(req)
    return out


def diff_new_requirements(
    requirements: list[str], approved_packages: list[str]
) -> list[str]:
    approved_names = {package_name(r) for r in approved_packages}
    new: list[str] = []
    for req in normalize_requirements(requirements):
        if package_name(req) not in approved_names:
            new.append(req)
    return new


async def runtime_health() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{TOOL_RUNTIME_URL}/health")
            if response.status_code == 200:
                return True, ""
            return False, response.text
    except httpx.RequestError as exc:
        return False, str(exc)


async def fetch_runtime_tools() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{TOOL_RUNTIME_URL}/tools")
        response.raise_for_status()
        return response.json().get("tools") or []


async def fetch_runtime_manifest() -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{TOOL_RUNTIME_URL}/manifest")
        response.raise_for_status()
        return response.json()


async def runtime_list_pip_packages() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{TOOL_RUNTIME_URL}/pip/packages")
        response.raise_for_status()
        return response.json().get("packages") or []


async def runtime_uninstall_pip_package(name: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.delete(
            f"{TOOL_RUNTIME_URL}/pip/packages/{name}",
        )
        if response.status_code != 200:
            detail = response.text
            try:
                detail = response.json().get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(detail)
        return response.json().get("packages") or []


async def runtime_run_tool(name: str, arguments: dict) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{TOOL_RUNTIME_URL}/tools/{name}/run",
            json={"arguments": arguments},
        )
        if response.status_code != 200:
            detail = response.text
            try:
                detail = response.json().get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(detail)
        data = response.json()
        return data.get("result", "")


async def runtime_pip_install(packages: list[str]) -> str:
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            f"{TOOL_RUNTIME_URL}/pip/install",
            json={"packages": packages},
        )
        if response.status_code != 200:
            raise RuntimeError(response.text)
        return response.json().get("logs", "")


async def runtime_install_tool(
    name: str,
    tool_code: str,
    test_code: str,
    requirements: list[str],
    *,
    skip_pip: bool = False,
) -> str:
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            f"{TOOL_RUNTIME_URL}/tools/{name}/install",
            json={
                "tool_code": tool_code,
                "test_code": test_code,
                "requirements": requirements,
                "skip_pip": skip_pip,
            },
        )
        if response.status_code != 200:
            detail = response.text
            try:
                detail = response.json().get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(detail)
        return response.json().get("logs", "")


async def runtime_verify_tool(name: str, test_code: str) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{TOOL_RUNTIME_URL}/tools/{name}/verify",
            json={"test_code": test_code},
        )
        if response.status_code != 200:
            raise RuntimeError(response.text)
        return response.json().get("logs", "")


async def runtime_delete_tool(name: str) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(f"{TOOL_RUNTIME_URL}/tools/{name}")
        if response.status_code not in (200, 404):
            response.raise_for_status()
