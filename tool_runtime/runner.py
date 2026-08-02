import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

TOOLS_DIR = Path(os.environ.get("TOOLS_DIR", "/app/custom_tools"))
VENV_PATH = Path(os.environ.get("VENV_PATH", "/app/venv"))
MANIFEST_PATH = TOOLS_DIR / ".venv_manifest.json"

_JSON_LITERAL_PATTERNS = (
    (re.compile(r"(?<=[:\[,])\s*false\b(?=\s*[,}\]])"), "False"),
    (re.compile(r"(?<=[:\[,])\s*true\b(?=\s*[,}\]])"), "True"),
    (re.compile(r"(?<=[:\[,])\s*null\b(?=\s*[,}\]])"), "None"),
)


def sanitize_python_json_literals(code: str) -> str:
    for pattern, replacement in _JSON_LITERAL_PATTERNS:
        code = pattern.sub(replacement, code)
    return code


def venv_python() -> Path:
    py = VENV_PATH / "bin" / "python"
    if not py.exists():
        py = VENV_PATH / "Scripts" / "python.exe"
    return py


def ensure_venv() -> None:
    py = venv_python()
    if py.exists():
        return
    VENV_PATH.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_PATH)],
        check=True,
        capture_output=True,
        text=True,
    )


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"approved_packages": []}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"approved_packages": []}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


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


def package_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[]", requirement.strip())[0].strip().lower()


def _pip_show_version(name: str) -> str | None:
    ensure_venv()
    py = venv_python()
    proc = subprocess.run(
        [str(py), "-m", "pip", "show", name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def list_installed_packages() -> list[dict]:
    manifest = load_manifest()
    packages: list[dict] = []
    seen_names: set[str] = set()
    for requirement in manifest.get("approved_packages") or []:
        name = package_name(requirement)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        version = _pip_show_version(name)
        entry: dict = {"requirement": requirement, "name": name}
        if version:
            entry["version"] = version
        packages.append(entry)
    return packages


def pip_install(requirements: list[str]) -> tuple[bool, str]:
    if not requirements:
        return True, "No packages to install."
    ensure_venv()
    py = venv_python()
    cmd = [str(py), "-m", "pip", "install", "--disable-pip-version-check", *requirements]
    logger.info("Running pip install: %s", " ".join(requirements))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, output
    manifest = load_manifest()
    approved = set(manifest.get("approved_packages") or [])
    approved.update(requirements)
    manifest["approved_packages"] = sorted(approved)
    save_manifest(manifest)
    return True, output


def pip_uninstall(package_name_str: str) -> tuple[bool, str]:
    name = package_name(package_name_str)
    if not name:
        return False, "Invalid package name."

    manifest = load_manifest()
    approved = manifest.get("approved_packages") or []
    matching = [req for req in approved if package_name(req) == name]
    if not matching:
        return False, f"Package '{name}' is not in the approved manifest."

    ensure_venv()
    py = venv_python()
    cmd = [str(py), "-m", "pip", "uninstall", "-y", name]
    logger.info("Running pip uninstall: %s", name)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, output

    remaining = [req for req in approved if package_name(req) != name]
    manifest["approved_packages"] = sorted(remaining)
    save_manifest(manifest)
    return True, output or f"Uninstalled {name}."


def _is_tool_module(file: Path) -> bool:
    name = file.name
    if name.startswith("__") or name.startswith("."):
        return False
    if name.endswith(".test.py"):
        return False
    return True


def _run_in_venv(script: str, *args: str, timeout: int = 120, allow_empty: bool = False) -> str:
    ensure_venv()
    py = venv_python()
    runner_path = TOOLS_DIR / ".venv_runner.py"
    try:
        runner_path.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [str(py), str(runner_path), *args],
            cwd=str(TOOLS_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
            raise RuntimeError(detail or f"venv runner exited with {proc.returncode}")
        output = (proc.stdout or "").strip()
        if not output and not allow_empty:
            raise RuntimeError("venv runner returned no output.")
        return output
    finally:
        if runner_path.exists():
            runner_path.unlink()


def list_tools() -> list[dict]:
    summaries: list[dict] = []
    ensure_venv()
    schema_script = """import importlib.util
import json
import sys
from pathlib import Path

tool_file = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(tool_file.stem, tool_file)
if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load {tool_file}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if not hasattr(mod, "get_tool_schema"):
    sys.exit(0)
print(json.dumps(mod.get_tool_schema()))
"""
    for file in sorted(TOOLS_DIR.glob("*.py")):
        if not _is_tool_module(file):
            continue
        try:
            raw = _run_in_venv(schema_script, str(file), timeout=30, allow_empty=True)
            if not raw:
                continue
            schema = json.loads(raw)
            fn = schema.get("function", schema)
            summaries.append({
                "name": fn.get("name", file.stem),
                "description": fn.get("description", ""),
                "schema": schema,
            })
        except Exception as exc:
            logger.warning("Skipping tool %s: %s", file.name, exc)
    return summaries


def run_tool(name: str, arguments: dict) -> str:
    file = TOOLS_DIR / f"{name}.py"
    if not file.exists():
        raise FileNotFoundError(f"Tool '{name}' not found.")
    run_script = """import importlib.util
import json
import sys
from pathlib import Path

tool_file = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(tool_file.stem, tool_file)
if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load {tool_file}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if not hasattr(mod, "run"):
    raise ValueError("Tool has no run() function.")
result = mod.run(**json.loads(sys.argv[2]))
if isinstance(result, str):
    print(result)
else:
    print(json.dumps(result))
"""
    return _run_in_venv(run_script, str(file), json.dumps(arguments))


def rewrite_workspace_paths(text: str, workspace_dir: Path) -> str:
    prefix = workspace_dir.resolve().as_posix()
    if not prefix.endswith("/"):
        prefix += "/"

    def _sub(match: re.Match[str]) -> str:
        path = match.group("path")
        return f'{match.group("q")}{prefix}{path}{match.group("q")}'

    text = re.sub(
        r'(?P<q>["\'])/workspace/(?P<path>[^"\']+\.py)(?P=q)',
        _sub, text,
    )
    text = re.sub(
        r'(?P<q>["\'])/workspace/skill_data/(?P<path>[^"\']+)(?P=q)',
        _sub, text,
    )
    text = re.sub(
        r'Path\(\s*(?P<q>["\'])/workspace/(?P<path>[^"\']+)(?P=q)\s*\)',
        lambda m: f'Path({m.group("q")}{prefix}{m.group("path")}{m.group("q")})',
        text,
    )
    return text


def verify_tool_in_runtime(tool_name: str, test_code: str) -> tuple[bool, str]:
    ensure_venv()
    py = venv_python()
    test_path = TOOLS_DIR / f".verify_{tool_name}_test_run.py"
    rewritten = rewrite_workspace_paths(test_code, TOOLS_DIR)
    try:
        test_path.write_text(rewritten, encoding="utf-8")
        proc = subprocess.run(
            [str(py), str(test_path)],
            cwd=str(TOOLS_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return False, output
        return True, output or "Runtime tests passed."
    finally:
        if test_path.exists():
            test_path.unlink()


def install_tool(tool_name: str, tool_code: str, test_code: str, requirements: list[str], *, skip_pip: bool = False) -> tuple[bool, str]:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    tool_code = sanitize_python_json_literals(tool_code)
    (TOOLS_DIR / f"{tool_name}.py").write_text(tool_code, encoding="utf-8")
    req_file = TOOLS_DIR / f"{tool_name}.requirements.txt"
    reqs = normalize_requirements(requirements)
    if reqs:
        req_file.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    elif req_file.exists():
        req_file.unlink()

    logs: list[str] = []
    if not skip_pip and reqs:
        ok, pip_log = pip_install(reqs)
        logs.append(pip_log)
        if not ok:
            return False, "\n".join(logs)

    ok, verify_log = verify_tool_in_runtime(tool_name, test_code)
    logs.append(verify_log)
    return ok, "\n".join(logs)


def delete_tool(tool_name: str) -> None:
    paths = [
        TOOLS_DIR / f"{tool_name}.py",
        TOOLS_DIR / f"{tool_name}.requirements.txt",
        TOOLS_DIR / f"{tool_name}.test.py",
        TOOLS_DIR / f"{tool_name}.manifest.json",
        TOOLS_DIR / "skill_data" / f"{tool_name}.json",
        TOOLS_DIR / f".verify_{tool_name}_test_run.py",
    ]
    for path in paths:
        path.unlink(missing_ok=True)

    pycache = TOOLS_DIR / "__pycache__"
    if pycache.is_dir():
        for pattern in (f"{tool_name}.cpython-*.pyc", f"{tool_name}.test.cpython-*.pyc"):
            for cached in pycache.glob(pattern):
                cached.unlink(missing_ok=True)
