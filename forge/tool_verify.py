"""Ephemeral venv verification for forged tools (replaces Docker sandbox).

Generated tool code runs in a throwaway venv under staging/. The venv and
workspace are deleted after each verify attempt. Works on Windows without Docker.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from forge.runtime_client import normalize_requirements, package_name
from forge.tools_engine import DEFAULT_SKILL_DATA

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STAGING_DIR = BASE_DIR / "staging"
STAGING_DIR.mkdir(exist_ok=True)
VERIFY_TIMEOUT = 120
PIP_TIMEOUT = 300
LOG_LIMIT = 8192
MISSING_MODULE_MAX_RETRIES = 4


def parse_missing_module(text: str) -> str | None:
    """Extract top-level PyPI package name from import errors in verify output."""
    if not text:
        return None
    patterns = (
        r"No module named '([^']+)'",
        r'No module named "([^"]+)"',
        r"ModuleNotFoundError: No module named ([^\s]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group(1).strip().strip("'\"")
        if not raw:
            continue
        return raw.split(".")[0]
    return None


def augment_requirements_for_missing_module(
    requirements: list[str],
    error_text: str,
) -> tuple[list[str], str | None]:
    """If error_text names a missing module, append it to requirements when new."""
    missing = parse_missing_module(error_text)
    if not missing:
        return requirements, None
    existing = {package_name(req) for req in requirements}
    if missing.lower() in existing:
        return requirements, None
    updated = normalize_requirements([*requirements, missing])
    return updated, missing


def _format_verify_logs(stdout: str, stderr: str, exit_code: int | None = None) -> str:
    parts = ["=== verification test output ==="]
    if exit_code is not None:
        parts.append(f"exit code: {exit_code}")
    if stdout.strip():
        parts.append("--- stdout ---")
        parts.append(stdout.strip())
    if stderr.strip():
        parts.append("--- stderr ---")
        parts.append(stderr.strip())
    combined = "\n".join(parts)
    if len(combined) > LOG_LIMIT:
        combined = combined[:LOG_LIMIT] + "\n… (truncated)"
    return combined


def _seed_skill_data(workspace_dir: Path, tool_name: str) -> Path:
    skill_data_dir = workspace_dir / "skill_data"
    skill_data_dir.mkdir(parents=True, exist_ok=True)
    data_path = skill_data_dir / f"{tool_name}.json"
    data_path.write_text(
        json.dumps(dict(DEFAULT_SKILL_DATA), indent=2) + "\n",
        encoding="utf-8",
    )
    return skill_data_dir


def rewrite_workspace_paths(text: str, workspace_dir: Path) -> str:
    """Rewrite /workspace/ paths used to load tools or touch files."""
    prefix = workspace_dir.resolve().as_posix()
    if not prefix.endswith("/"):
        prefix += "/"

    def _sub(match: re.Match[str]) -> str:
        path = match.group("path")
        return f'{match.group("q")}{prefix}{path}{match.group("q")}'

    # Tool module loads: "/workspace/{name}.py"
    text = re.sub(
        r'(?P<q>["\'])/workspace/(?P<path>[^"\']+\.py)(?P=q)',
        _sub,
        text,
    )
    # skill_data persistence paths
    text = re.sub(
        r'(?P<q>["\'])/workspace/skill_data/(?P<path>[^"\']+)(?P=q)',
        _sub,
        text,
    )
    # Path("/workspace/...") for file operations in tests
    text = re.sub(
        r'Path\(\s*(?P<q>["\'])/workspace/(?P<path>[^"\']+)(?P=q)\s*\)',
        lambda m: f'Path({m.group("q")}{prefix}{m.group("path")}{m.group("q")})',
        text,
    )
    return text


def _venv_python(venv_dir: Path) -> Path:
    py = venv_dir / "Scripts" / "python.exe"
    if py.exists():
        return py
    return venv_dir / "bin" / "python"


def _venv_site_packages(venv_dir: Path) -> Path | None:
    """Return site-packages for a venv if it exists."""
    candidates = [
        venv_dir / "Lib" / "site-packages",
        venv_dir / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return None


def _contract_runner_env(venv_dir: Path) -> dict[str, str]:
    """Host Python runs contract logic; tool deps resolve from verify venv site-packages."""
    env = os.environ.copy()
    site_packages = _venv_site_packages(venv_dir)
    if site_packages is not None:
        prefix = str(site_packages)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = prefix if not existing else f"{prefix}{os.pathsep}{existing}"
    return env


def _pip_install(venv_dir: Path, requirements: list[str]) -> tuple[bool, str]:
    reqs = normalize_requirements(requirements)
    if not reqs:
        return True, "No packages to install."
    py = _venv_python(venv_dir)
    cmd = [
        str(py),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        *reqs,
    ]
    logger.info("Verify venv pip install: %s", " ".join(reqs))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=PIP_TIMEOUT,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, output or f"pip install failed with exit code {proc.returncode}"
    return True, output or "Packages installed."


def _create_verify_venv(venv_dir: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return False, detail or "Failed to create verify venv."
    return True, ""


def _run_contract_test_subprocess(
    venv_dir: Path,
    workspace_dir: Path,
    tool_name: str,
    manifest: dict,
) -> tuple[bool, str]:
    manifest_path = workspace_dir / ".contract_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runner_path = workspace_dir / ".contract_runner.py"
    runner_path.write_text(
        f'''import json
import sys
from pathlib import Path

sys.path.insert(0, {str(BASE_DIR)!r})

from forge.tools_engine import verify_skill_api_contract

tool_name = {tool_name!r}
tool_code = Path("{tool_name}.py").read_text(encoding="utf-8")
manifest = json.loads(Path(".contract_manifest.json").read_text(encoding="utf-8"))

ok, reason = verify_skill_api_contract(tool_name, tool_code, manifest)
if ok:
    print(reason or "API contract tests passed.")
    raise SystemExit(0)
print(reason or "API contract tests failed.", file=sys.stderr)
raise SystemExit(1)
''',
        encoding="utf-8",
    )
    py = _venv_python(venv_dir)
    proc = subprocess.run(
        [sys.executable, str(runner_path)],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
        timeout=VERIFY_TIMEOUT,
        env=_contract_runner_env(venv_dir),
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode == 0:
        return True, stdout.strip() or "API contract tests passed."
    combined = _format_verify_logs(stdout, stderr, proc.returncode)
    return False, combined


def verify_skill_api_contract_in_ephemeral_venv(
    tool_name: str,
    tool_code: str,
    manifest: dict | None,
    requirements: list[str],
) -> tuple[bool, str, list[str]]:
    """Run interactive API contract tests in an isolated venv with requirements installed."""
    if not manifest or manifest.get("kind") != "interactive":
        return True, "", list(requirements)

    current_requirements = normalize_requirements(requirements)
    last_output = ""

    for attempt in range(MISSING_MODULE_MAX_RETRIES):
        verify_root = STAGING_DIR / f".contract_{tool_name}_{uuid.uuid4().hex[:12]}"
        workspace_dir = verify_root / "workspace"
        venv_dir = verify_root / ".venv"

        try:
            workspace_dir.mkdir(parents=True, exist_ok=True)
            (workspace_dir / f"{tool_name}.py").write_text(tool_code, encoding="utf-8")
            _seed_skill_data(workspace_dir, tool_name)

            ok, detail = _create_verify_venv(venv_dir)
            if not ok:
                last_output = _format_verify_logs("", detail)
                return False, last_output, current_requirements

            pip_ok, pip_log = _pip_install(venv_dir, current_requirements)
            if not pip_ok:
                last_output = _format_verify_logs("", pip_log)
                return False, last_output, current_requirements

            contract_ok, contract_output = _run_contract_test_subprocess(
                venv_dir,
                workspace_dir,
                tool_name,
                manifest,
            )
            if contract_ok:
                return True, contract_output, current_requirements

            last_output = contract_output
            updated, missing = augment_requirements_for_missing_module(
                current_requirements,
                contract_output,
            )
            if missing and updated != current_requirements:
                logger.info(
                    "Contract test missing %s — adding to verify requirements (attempt %s)",
                    missing,
                    attempt + 1,
                )
                current_requirements = updated
                continue
            return False, last_output, current_requirements
        except subprocess.TimeoutExpired:
            return False, _format_verify_logs("", "Contract verification timed out."), current_requirements
        except Exception as exc:
            logger.exception("Contract verify failed for %s", tool_name)
            return False, _format_verify_logs("", str(exc)), current_requirements
        finally:
            shutil.rmtree(verify_root, ignore_errors=True)

    return False, last_output or "Contract verification failed.", current_requirements


def verify_tool_in_ephemeral_venv(
    tool_name: str,
    tool_code: str,
    test_code: str,
    requirements: list[str],
    *,
    manifest: dict | None = None,
) -> tuple[bool, str]:
    verify_root = STAGING_DIR / f".verify_{tool_name}_{uuid.uuid4().hex[:12]}"
    workspace_dir = verify_root / "workspace"
    venv_dir = verify_root / ".venv"

    try:
        workspace_dir.mkdir(parents=True, exist_ok=True)

        (workspace_dir / f"{tool_name}.py").write_text(tool_code, encoding="utf-8")

        if manifest and manifest.get("kind") == "interactive":
            _seed_skill_data(workspace_dir, tool_name)

        rewritten_test = rewrite_workspace_paths(test_code, workspace_dir)
        test_path = workspace_dir / "test_run.py"
        test_path.write_text(rewritten_test, encoding="utf-8")

        ok, detail = _create_verify_venv(venv_dir)
        if not ok:
            return False, _format_verify_logs("", detail)

        ok, pip_log = _pip_install(venv_dir, requirements)
        if not ok:
            return False, _format_verify_logs("", pip_log)

        py = _venv_python(venv_dir)
        test_proc = subprocess.run(
            [str(py), str(test_path)],
            cwd=str(workspace_dir),
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT,
        )
        stdout = test_proc.stdout or ""
        stderr = test_proc.stderr or ""
        if pip_log.strip() and "No packages to install" not in pip_log:
            stderr = f"--- pip install ---\n{pip_log.strip()}\n\n{stderr}"

        if test_proc.returncode != 0:
            return False, _format_verify_logs(stdout, stderr, test_proc.returncode)

        return True, stdout or "Tests passed."
    except subprocess.TimeoutExpired:
        return False, _format_verify_logs("", "Verification timed out.")
    except Exception as exc:
        logger.exception("Ephemeral verify failed for %s", tool_name)
        return False, _format_verify_logs("", str(exc))
    finally:
        shutil.rmtree(verify_root, ignore_errors=True)
