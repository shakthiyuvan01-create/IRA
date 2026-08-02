"""Dynamic tool loading, execution, manifest management, and interactive skill support.

Port of Ada-SI's tools_engine.py adapted for IRA.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import shutil
import types
from pathlib import Path

from forge.prompts_config import (
    get_forge_runtime_context,
    get_tool_edit_existing_description,
    get_tool_generate_new_description,
    get_tool_propose_batch_description,
)
from forge.runtime_client import (
    diff_new_requirements,
    fetch_runtime_manifest,
    fetch_runtime_tools,
    normalize_requirements,
    package_name,
    runtime_delete_tool,
    runtime_health,
    runtime_run_tool,
    set_runtime_url,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BASE_DIR / "custom_tools"
TOOLS_DIR.mkdir(exist_ok=True)
STAGING_DIR = BASE_DIR / "staging"
SKILL_DATA_DIR = TOOLS_DIR / "skill_data"
SKILL_DATA_DIR.mkdir(exist_ok=True)

VALID_SKILL_KINDS = frozenset({"headless", "interactive"})
VALID_UI_TEMPLATES = frozenset({"calendar", "list", "table", "custom"})
UI_ACTION_KEYS = frozenset({"fetch", "create", "delete", "toggle", "update"})
UI_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
MAX_UI_FILES = 20
MAX_UI_BUNDLE_BYTES = 512 * 1024
DEFAULT_SKILL_DATA: dict = {"records": []}


def _validate_tool_name(tool_name: str) -> None:
    if not tool_name or not tool_name.replace("_", "").isalnum():
        raise ValueError(f"Invalid tool name: {tool_name}")


def read_tool_manifest(tool_name: str) -> dict | None:
    path = TOOLS_DIR / f"{tool_name}.manifest.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        logger.warning("Invalid manifest JSON for %s", tool_name)
        return None


def _find_operation(operations: list[str], *needles: str) -> str | None:
    for op in operations:
        lowered = op.lower()
        if any(needle in lowered for needle in needles):
            return op
    return None


def _is_custom_ui(manifest: dict) -> bool:
    ui = manifest.get("ui") or {}
    return ui.get("template") == "custom"


def normalize_interactive_manifest(manifest: dict, tool_name: str) -> dict:
    """Fill missing interactive manifest fields the forge often omits."""
    if manifest.get("kind") != "interactive":
        return manifest

    ui = manifest.get("ui")
    if not isinstance(ui, dict):
        ui = {}
        manifest["ui"] = ui

    operations = manifest.get("operations")
    ops: list[str] = []
    if isinstance(operations, list) and operations:
        ops = [str(op) for op in operations if str(op).strip()]

    if not ops:
        actions = ui.get("actions")
        if isinstance(actions, dict):
            inferred_ops: list[str] = []
            seen: set[str] = set()
            for action_name in actions.values():
                if not isinstance(action_name, str):
                    continue
                name = action_name.strip()
                if name and name not in seen:
                    seen.add(name)
                    inferred_ops.append(name)
            if inferred_ops:
                ops = inferred_ops
                manifest["operations"] = ops

    if not ops:
        return manifest

    manifest["operations"] = ops

    if _is_custom_ui(manifest):
        return manifest

    template = ui.get("template", "list")
    if template == "list":
        ui.setdefault("title_field", "title")
        ui.setdefault("done_field", "done")
    elif template == "calendar":
        ui.setdefault("title_field", "title")
        ui.setdefault("date_field", "start")
        ui.setdefault("end_date_field", "end")

    actions = ui.get("actions")
    if isinstance(actions, dict) and actions:
        return manifest

    inferred: dict[str, str] = {}
    fetch_op = _find_operation(ops, "list_")
    create_op = _find_operation(ops, "add_", "create_")
    delete_op = _find_operation(ops, "delete_")
    toggle_op = _find_operation(ops, "complete_", "toggle_", "update_")

    if fetch_op:
        inferred["fetch"] = fetch_op
    if create_op:
        inferred["create"] = create_op
    if delete_op:
        inferred["delete"] = delete_op
    if toggle_op and template == "list":
        inferred["toggle"] = toggle_op

    if inferred:
        ui["actions"] = inferred
    return manifest


def validate_manifest(manifest: dict, tool_name: str) -> tuple[bool, str]:
    if not isinstance(manifest, dict):
        return False, "manifest must be a JSON object."
    kind = manifest.get("kind", "headless")
    if kind not in VALID_SKILL_KINDS:
        return False, f"manifest.kind must be one of {sorted(VALID_SKILL_KINDS)}."
    if kind != "interactive":
        return True, ""
    normalize_interactive_manifest(manifest, tool_name)
    ui = manifest.get("ui")
    if not isinstance(ui, dict):
        return False, "Interactive skills require manifest.ui object."
    template = ui.get("template", "")
    if template not in VALID_UI_TEMPLATES:
        return False, f"manifest.ui.template must be one of {sorted(VALID_UI_TEMPLATES)}."
    custom = _is_custom_ui(manifest)
    if template == "custom":
        entry = ui.get("entry", "index.html")
        if not isinstance(entry, str) or not entry.strip():
            return False, "Custom interactive skills require manifest.ui.entry (e.g. index.html)."
        if not UI_FILENAME_PATTERN.match(entry.strip()):
            return False, f"Invalid manifest.ui.entry filename: {entry!r}"
        ui["entry"] = entry.strip()
    actions = ui.get("actions")
    if not isinstance(actions, dict):
        return False, "Interactive skills require manifest.ui.actions object."
    operations = manifest.get("operations")
    if not isinstance(operations, list) or not operations:
        return False, "Interactive skills require manifest.operations list."
    operations_set = {str(op) for op in operations}
    for key, action_name in actions.items():
        if custom:
            if not isinstance(key, str) or not key.strip():
                return False, "manifest.ui.actions keys must be non-empty strings."
        elif key not in UI_ACTION_KEYS:
            return False, f"Unknown manifest.ui.actions key: {key!r}"
        if not isinstance(action_name, str) or not action_name.strip():
            return False, f"manifest.ui.actions.{key} must be a non-empty string."
        if action_name not in operations_set:
            return (
                False,
                f"manifest.ui.actions.{key} ({action_name!r}) must be listed in manifest.operations.",
            )
    if custom and not actions:
        return False, "Custom interactive skills require at least one manifest.ui.actions entry."
    template_required = {
        "list": {"create", "delete", "toggle"},
        "calendar": {"create", "delete"},
        "table": {"create", "delete"},
        "custom": set(),
    }
    required = template_required.get(template, set())
    missing = required - set(actions.keys())
    if missing:
        return (
            False,
            f"manifest.ui.actions missing required keys for template {template!r}: "
            f"{sorted(missing)}",
        )
    if not manifest.get("display_name"):
        manifest["display_name"] = tool_name.replace("_", " ").title()
    return True, ""


def skill_ui_dir(tool_name: str) -> Path:
    _validate_tool_name(tool_name)
    return TOOLS_DIR / "ui" / tool_name


def validate_ui_files(
    ui_files: dict[str, str] | None,
    manifest: dict | None,
    tool_name: str,
) -> tuple[bool, str]:
    if not manifest or manifest.get("kind") != "interactive":
        return True, ""
    ui = manifest.get("ui") or {}
    if ui.get("template") != "custom":
        if ui_files:
            return False, "ui_files is only allowed when manifest.ui.template is custom."
        return True, ""
    if not ui_files:
        return False, "Custom interactive skills require ui_files with at least index.html."
    if len(ui_files) > MAX_UI_FILES:
        return False, f"ui_files exceeds maximum of {MAX_UI_FILES} files."
    entry = ui.get("entry", "index.html")
    total_bytes = 0
    for name, content in ui_files.items():
        if not isinstance(name, str) or not UI_FILENAME_PATTERN.match(name):
            return False, f"Invalid ui_files key: {name!r}"
        if not isinstance(content, str):
            return False, f"ui_files[{name!r}] must be a string."
        total_bytes += len(content.encode("utf-8"))
    if total_bytes > MAX_UI_BUNDLE_BYTES:
        return False, f"ui_files total size exceeds {MAX_UI_BUNDLE_BYTES} bytes."
    if entry not in ui_files:
        return False, f"ui_files must include entry file {entry!r}."
    js_ok, js_reason = validate_ui_js(ui_files, manifest)
    if not js_ok:
        return False, js_reason
    return True, ""


def validate_ui_js(
    ui_files: dict[str, str] | None,
    manifest: dict | None,
) -> tuple[bool, str]:
    """Static lint for custom iframe app.js SDK usage."""
    if not manifest or manifest.get("kind") != "interactive":
        return True, ""
    ui = manifest.get("ui") or {}
    if ui.get("template") != "custom":
        return True, ""
    if not ui_files:
        return True, ""

    html = ui_files.get("index.html", "")
    if "/static/skill-sdk.js" not in html:
        return False, "index.html must load /static/skill-sdk.js"

    app_js = ui_files.get("app.js", "")
    if not app_js.strip():
        return False, "ui_files must include non-empty app.js for custom template."
    if re.search(r"\bnew\s+AdaSkill\s*\(", app_js):
        return False, "app.js must not use new AdaSkill() — use AdaSkill singleton."
    if re.search(r"\bAdaSkill\.call\s*\(\s*\{", app_js) or re.search(
        r"\bskill\.call\s*\(\s*\{", app_js
    ):
        return False, "Use AdaSkill.call('action_name', { params }) — action must be a string."
    if "AdaSkill.init" not in app_js:
        return False, "app.js must call AdaSkill.init()."
    if not re.search(r"\bAdaSkill\.call\s*\(", app_js) and "getData" not in app_js:
        return False, "app.js must call AdaSkill.call() or use getData()."
    return True, ""


def _id_param_for_delete(operations: list[str], template: str) -> str:
    for op in operations:
        lowered = op.lower()
        if "delete" not in lowered:
            continue
    if template == "list":
        return "task_id"
    if template == "calendar":
        return "event_id"
    return "id"


_CONTRACT_SAMPLES: dict[str, str] = {
    "title": "Contract Test",
    "body": "Contract test body",
    "name": "Contract Test",
    "email": "contract@example.com",
    "phone": "555-0100",
    "start": "2026-01-01T10:00:00Z",
    "end": "2026-01-01T11:00:00Z",
    "due_date": "2026-01-01",
    "description": "Contract test description",
    "content": "Contract test content",
    "text": "Contract test text",
}


def _tool_schema_properties(mod) -> dict[str, dict]:
    if not hasattr(mod, "get_tool_schema"):
        return {}
    schema = mod.get_tool_schema()
    if not isinstance(schema, dict):
        return {}
    fn = schema.get("function", schema)
    params = fn.get("parameters") if isinstance(fn, dict) else {}
    if not isinstance(params, dict):
        return {}
    props = params.get("properties", {})
    return props if isinstance(props, dict) else {}


def _tool_schema_required(mod) -> list[str]:
    if not hasattr(mod, "get_tool_schema"):
        return []
    schema = mod.get_tool_schema()
    if not isinstance(schema, dict):
        return []
    fn = schema.get("function", schema)
    params = fn.get("parameters") if isinstance(fn, dict) else {}
    if not isinstance(params, dict):
        return []
    required = params.get("required", [])
    if not isinstance(required, list):
        return []
    return [str(k) for k in required if str(k) != "action"]


def _manifest_field_keys(manifest: dict) -> list[str]:
    ui = manifest.get("ui") or {}
    keys: list[str] = []
    for field in ui.get("fields") or []:
        if isinstance(field, dict):
            key = field.get("key")
            if isinstance(key, str) and key.strip():
                keys.append(key.strip())
    for attr in ("title_field", "date_field", "end_date_field"):
        value = ui.get(attr)
        if isinstance(value, str) and value.strip() and value not in keys:
            keys.append(value.strip())
    return keys


def _create_param_keys(mod, manifest: dict) -> list[str]:
    skip = frozenset({"action", "task_id", "event_id", "note_id", "contact_id", "id", "done"})
    keys: list[str] = []
    seen: set[str] = set()

    def _add(key: str) -> None:
        if key in skip or key in seen:
            return
        seen.add(key)
        keys.append(key)

    for key in _manifest_field_keys(manifest):
        _add(key)
    for key in _tool_schema_required(mod):
        _add(key)
    for key in _tool_schema_properties(mod):
        if key.endswith("_id"):
            continue
        _add(key)

    # Calendar templates always need start/end when present in schema or manifest.
    ui = manifest.get("ui") or {}
    if ui.get("template") == "calendar":
        for key in ("title", "start", "end"):
            _add(key)

    if not keys:
        template = ui.get("template", "list")
        if template == "calendar":
            return ["title", "start", "end"]
        if template == "table":
            return ["name", "email", "phone"]
        return ["title", "body"] if ui.get("template") == "custom" else ["title"]
    return keys


def _sample_value_for_field(key: str) -> str:
    if key in _CONTRACT_SAMPLES:
        return _CONTRACT_SAMPLES[key]
    if key.endswith("_date") or key == "due_date":
        return "2026-01-01"
    if key.endswith("_at"):
        return "2026-01-01T12:00:00Z"
    return f"contract_{key}"


def _contract_create_params(mod, manifest: dict) -> dict[str, str]:
    params: dict[str, str] = {}
    for key in _create_param_keys(mod, manifest):
        if key == "done":
            continue
        params[key] = _sample_value_for_field(key)
    return params


def _delete_id_param(mod, delete_action: str, operations: list[str], template: str) -> str:
    props = _tool_schema_properties(mod)
    for candidate in ("task_id", "event_id", "note_id", "contact_id", "id"):
        if candidate in props:
            return candidate
    action_lower = delete_action.lower()
    if "note" in action_lower:
        return "note_id"
    if "task" in action_lower:
        return "task_id"
    if "event" in action_lower:
        return "event_id"
    if "contact" in action_lower:
        return "contact_id" if "contact_id" in props else "id"
    return _id_param_for_delete(operations, template)


def _contract_minimal_params(mod) -> dict[str, str]:
    params: dict[str, str] = {}
    for key in _tool_schema_required(mod):
        if key == "action":
            continue
        params[key] = _sample_value_for_field(key)
    return params


def _verify_crud_contract(
    mod,
    manifest: dict,
    data_path: Path,
    actions: dict,
    operations: list[str],
    template: str,
) -> tuple[bool, str]:
    create_action = actions.get("create")
    delete_action = actions.get("delete")
    fetch_action = actions.get("fetch")
    created_id: str | None = None

    if create_action:
        params: dict = {"action": create_action}
        params.update(_contract_create_params(mod, manifest))
        result = mod.run(**params)
        if isinstance(result, dict) and result.get("error"):
            return False, f"create action {create_action!r} failed: {result['error']}"
        data = json.loads(data_path.read_text(encoding="utf-8"))
        records = data.get("records", [])
        if not records:
            return False, f"create action {create_action!r} did not persist a record."
        last = records[-1]
        created_id = str(last.get("id", "")) or None

    if fetch_action:
        result = mod.run(action=fetch_action)
        if isinstance(result, dict) and result.get("error"):
            return False, f"fetch action {fetch_action!r} failed: {result['error']}"

    if delete_action and created_id:
        id_key = _delete_id_param(mod, delete_action, operations, template)
        result = mod.run(action=delete_action, **{id_key: created_id})
        if isinstance(result, dict) and result.get("error"):
            return False, f"delete action {delete_action!r} failed: {result['error']}"
        data = json.loads(data_path.read_text(encoding="utf-8"))
        if data.get("records"):
            return False, f"delete action {delete_action!r} did not remove the record."

    return True, "API contract tests passed."


def _operation_category(op: str) -> str:
    lowered = op.lower()
    if any(needle in lowered for needle in ("list_", "fetch", "get_state")) or (
        lowered.startswith("get_") and "delete" not in lowered
    ):
        return "read"
    if any(needle in lowered for needle in ("add_", "create_", "insert_")):
        return "create"
    if any(needle in lowered for needle in ("delete_", "remove_")):
        return "delete"
    if any(needle in lowered for needle in ("complete_", "toggle_", "update_")):
        return "update"
    if any(needle in lowered for needle in ("start", "stop", "pause", "reset")):
        return "control"
    return "generic"


def _sort_operations_for_contract(operations: list[str]) -> list[str]:
    order = {"read": 0, "control": 1, "create": 2, "update": 3, "delete": 4, "generic": 5}
    unique = []
    seen: set[str] = set()
    for op in operations:
        name = str(op).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return sorted(unique, key=lambda op: (order.get(_operation_category(op), 99), op))


def _contract_params_for_operation(
    mod,
    manifest: dict,
    op: str,
    category: str,
    created_id: str | None,
) -> dict[str, str]:
    props = _tool_schema_properties(mod)
    if category == "create":
        return _contract_create_params(mod, manifest)
    if category in ("update", "delete"):
        if not created_id:
            return {}
        id_key = _delete_id_param(mod, op, manifest.get("operations") or [], "custom")
        if id_key in props:
            return {id_key: created_id}
        return {}
    if category in ("read", "control"):
        return {}
    params: dict[str, str] = {}
    for key in props:
        if key in ("action",) or key.endswith("_id"):
            continue
        params[key] = _sample_value_for_field(key)
    return params


def _extract_created_id(result, data_path: Path) -> str | None:
    if isinstance(result, dict):
        record_id = result.get("id")
        if record_id:
            return str(record_id)
    if data_path.exists():
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
            records = data.get("records", [])
            if records:
                last = records[-1]
                if isinstance(last, dict) and last.get("id"):
                    return str(last["id"])
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _verify_custom_contract(
    mod,
    manifest: dict,
    operations: list[str],
    data_path: Path,
) -> tuple[bool, str]:
    created_id: str | None = None
    tested = 0
    for op in _sort_operations_for_contract(operations):
        category = _operation_category(op)
        if category in ("update", "delete") and not created_id:
            continue
        params = _contract_params_for_operation(mod, manifest, op, category, created_id)
        result = mod.run(action=op, **params)
        tested += 1
        if isinstance(result, dict) and result.get("error"):
            return False, f"action {op!r} failed: {result['error']}"
        if category == "create":
            created_id = _extract_created_id(result, data_path) or created_id
    if tested == 0:
        return False, "Custom contract test found no operations to exercise."
    return True, "API contract tests passed."


def verify_skill_api_contract(
    tool_name: str,
    tool_code: str,
    manifest: dict | None,
) -> tuple[bool, str]:
    """Exercise run(action=...) for manifest.ui.actions using a temp skill_data file."""
    if not manifest or manifest.get("kind") != "interactive":
        return True, ""
    normalize_interactive_manifest(manifest, tool_name)
    ui = manifest.get("ui") or {}
    actions = ui.get("actions") or {}
    operations = [str(op) for op in (manifest.get("operations") or [])]
    template = ui.get("template", "list")

    import importlib.util
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_data_dir = root / "skill_data"
            skill_data_dir.mkdir(parents=True, exist_ok=True)
            data_path = skill_data_dir / f"{tool_name}.json"
            data_path.write_text(json.dumps(DEFAULT_SKILL_DATA, indent=2), encoding="utf-8")

            tool_path = root / f"{tool_name}.py"
            patched_code = tool_code.replace(
                'Path(__file__).parent / "skill_data"',
                f'Path(r"{skill_data_dir.as_posix()}")',
            ).replace(
                "Path(__file__).parent / 'skill_data'",
                f'Path(r"{skill_data_dir.as_posix()}")',
            )
            tool_path.write_text(patched_code, encoding="utf-8")

            spec = importlib.util.spec_from_file_location(tool_name, tool_path)
            if spec is None or spec.loader is None:
                return False, "Cannot load tool module for contract test."
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            if _is_custom_ui(manifest):
                return _verify_custom_contract(mod, manifest, operations, data_path)
            return _verify_crud_contract(
                mod, manifest, data_path, actions, operations, template
            )
    except Exception as exc:
        return False, f"API contract test error: {exc}"


def write_ui_files(
    tool_name: str,
    ui_files: dict[str, str] | None,
    manifest: dict | None,
) -> None:
    ui = (manifest or {}).get("ui") or {}
    if ui.get("template") == "custom" and ui_files:
        target_dir = skill_ui_dir(tool_name)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, content in ui_files.items():
            (target_dir / name).write_text(content, encoding="utf-8")
    elif skill_ui_dir(tool_name).exists():
        shutil.rmtree(skill_ui_dir(tool_name), ignore_errors=True)


def remove_ui_files(tool_name: str) -> None:
    ui_dir = skill_ui_dir(tool_name)
    if ui_dir.exists():
        shutil.rmtree(ui_dir, ignore_errors=True)


def skill_ui_entry_path(tool_name: str) -> Path | None:
    manifest = read_tool_manifest(tool_name)
    if not manifest or manifest.get("kind") != "interactive":
        return None
    ui = manifest.get("ui") or {}
    if ui.get("template") != "custom":
        return None
    entry = ui.get("entry", "index.html")
    path = skill_ui_dir(tool_name) / entry
    return path if path.is_file() else None


def resolve_skill_ui_file(tool_name: str, file_path: str) -> Path | None:
    manifest = read_tool_manifest(tool_name)
    if not manifest or manifest.get("kind") != "interactive":
        return None
    ui = manifest.get("ui") or {}
    if ui.get("template") != "custom":
        return None
    base = skill_ui_dir(tool_name).resolve()
    if not base.is_dir():
        return None
    candidate = (base / file_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def ui_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")


def write_tool_manifest(tool_name: str, manifest: dict) -> Path:
    _validate_tool_name(tool_name)
    ok, reason = validate_manifest(manifest, tool_name)
    if not ok:
        raise ValueError(reason)
    path = TOOLS_DIR / f"{tool_name}.manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def skill_data_path(tool_name: str) -> Path:
    _validate_tool_name(tool_name)
    return SKILL_DATA_DIR / f"{tool_name}.json"


def is_interactive_skill(tool_name: str) -> bool:
    manifest = read_tool_manifest(tool_name)
    return manifest is not None and manifest.get("kind") == "interactive"


def read_skill_data(tool_name: str) -> dict:
    if not is_interactive_skill(tool_name):
        raise ValueError(f"Skill '{tool_name}' is not an interactive skill.")
    path = skill_data_path(tool_name)
    if not path.exists():
        return dict(DEFAULT_SKILL_DATA)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(DEFAULT_SKILL_DATA)
    except json.JSONDecodeError:
        logger.warning("Invalid skill data JSON for %s — resetting", tool_name)
        return dict(DEFAULT_SKILL_DATA)


def write_skill_data(tool_name: str, data: dict) -> None:
    if not is_interactive_skill(tool_name):
        raise ValueError(f"Skill '{tool_name}' is not an interactive skill.")
    if not isinstance(data, dict):
        raise ValueError("Skill data must be a JSON object.")
    path = skill_data_path(tool_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _summary_from_manifest(tool_name: str, description: str) -> dict:
    summary: dict = {"name": tool_name, "description": description, "kind": "headless"}
    manifest = read_tool_manifest(tool_name)
    if not manifest:
        return summary
    kind = manifest.get("kind", "headless")
    summary["kind"] = kind
    if manifest.get("display_name"):
        summary["display_name"] = manifest["display_name"]
    if manifest.get("icon"):
        summary["icon"] = manifest["icon"]
    ui = manifest.get("ui")
    if isinstance(ui, dict) and ui:
        summary["ui"] = ui
    if manifest.get("operations"):
        summary["operations"] = manifest["operations"]
    return summary


def build_propose_tool_batch_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "propose_tool_batch",
            "description": get_tool_propose_batch_description(),
            "parameters": {
                "type": "object",
                "properties": {
                    "tools": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 10,
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool_name": {
                                    "type": "string",
                                    "description": "Snake_case name for the new tool module.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "Detailed explanation of what the tool should do."
                                    ),
                                },
                            },
                            "required": ["tool_name", "description"],
                        },
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Short user-facing summary of the proposed multi-tool batch."
                        ),
                    },
                },
                "required": ["tools", "summary"],
            },
        },
    }


def build_generate_new_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "generate_new_tool",
            "description": get_tool_generate_new_description(),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Snake_case name for the new tool module.",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Detailed explanation of what the tool should do, its inputs, "
                            "and expected outputs."
                        ),
                    },
                },
                "required": ["tool_name", "description"],
            },
        },
    }


def build_open_skill_app_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "open_skill_app",
            "description": (
                "Open an installed interactive skill as a popup mini-app in the UI. "
                "Use when the user asks to see, view, open, or show an interactive skill "
                "(calendar, todo list, tracker, etc.) rather than only querying its data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Snake_case name of the installed interactive skill.",
                    },
                },
                "required": ["skill_name"],
            },
        },
    }


def build_edit_existing_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "edit_existing_tool",
            "description": get_tool_edit_existing_description(),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Snake_case name of the existing installed tool.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description of the requested changes.",
                    },
                },
                "required": ["tool_name", "description"],
            },
        },
    }


def read_tool_file(tool_name: str) -> str:
    path = TOOLS_DIR / f"{tool_name}.py"
    if not path.exists():
        raise FileNotFoundError(f"Tool '{tool_name}' not found.")
    return path.read_text(encoding="utf-8")


def read_tool_requirements(tool_name: str) -> list[str]:
    path = TOOLS_DIR / f"{tool_name}.requirements.txt"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return normalize_requirements([line.strip() for line in lines if line.strip()])


def get_package_usage() -> dict[str, list[str]]:
    """Map normalized package name to tool stems that declare it in requirements."""
    usage: dict[str, list[str]] = {}
    for req_path in TOOLS_DIR.glob("*.requirements.txt"):
        tool_name = req_path.name.removesuffix(".requirements.txt")
        for req in read_tool_requirements(tool_name):
            key = package_name(req)
            if not key:
                continue
            usage.setdefault(key, [])
            if tool_name not in usage[key]:
                usage[key].append(tool_name)
    for tools in usage.values():
        tools.sort()
    return usage


def tool_exists(tool_name: str) -> bool:
    return (TOOLS_DIR / f"{tool_name}.py").exists()


_JSON_LITERAL_PATTERNS = (
    (re.compile(r"(?<=[:\[,])\s*false\b(?=\s*[,}\]])"), "False"),
    (re.compile(r"(?<=[:\[,])\s*true\b(?=\s*[,}\]])"), "True"),
    (re.compile(r"(?<=[:\[,])\s*null\b(?=\s*[,}\]])"), "None"),
)


def sanitize_python_json_literals(code: str) -> str:
    """Replace JSON-style true/false/null that LLMs often emit inside Python dict literals."""
    for pattern, replacement in _JSON_LITERAL_PATTERNS:
        code = pattern.sub(replacement, code)
    return code


def _exec_tool_module_from_source(source: str, module_name: str):
    source = sanitize_python_json_literals(source)
    mod = types.ModuleType(module_name)
    mod.__file__ = f"{module_name}.py"
    exec(compile(source, mod.__file__, "exec"), mod.__dict__)
    return mod


def _schema_name_and_description(schema: dict) -> tuple[str, str]:
    fn = schema.get("function", schema)
    return fn.get("name", ""), fn.get("description", "")


_TTS_SYSTEM_DIRECTIVE = (
    "Voice output is ON. Your reply will be read aloud via text-to-speech. "
    "Keep answers concise and speakable: use plain sentences only in flowing prose. "
    "Do not use bullet points, numbered lists, headings, markdown, tables, or code blocks "
    "unless the user explicitly asks for structured or code output. "
    "Prefer short paragraphs of full sentences over long monologues."
)


def _load_local_schemas() -> list[dict]:
    schemas: list[dict] = []
    for file in sorted(TOOLS_DIR.glob("*.py")):
        if file.name.startswith("__") or file.name.endswith(".test.py"):
            continue
        try:
            mod = _exec_tool_module_from_source(
                file.read_text(encoding="utf-8"), file.stem
            )
            if hasattr(mod, "get_tool_schema"):
                schemas.append(mod.get_tool_schema())
        except Exception as exc:
            logger.warning("Local schema load failed for %s: %s", file.name, exc)
    return schemas


def load_dynamic_tools() -> list[dict]:
    tools = [
        build_generate_new_tool_schema(),
        build_propose_tool_batch_schema(),
        build_edit_existing_tool_schema(),
        build_open_skill_app_schema(),
    ]
    try:
        runtime_tools = asyncio.get_event_loop().run_until_complete(fetch_runtime_tools())
        for item in runtime_tools:
            schema = item.get("schema")
            if schema:
                tools.append(schema)
        return tools
    except RuntimeError:
        pass
    except Exception as exc:
        logger.warning("Runtime tool list failed, falling back to local: %s", exc)

    try:
        loop = asyncio.new_event_loop()
        runtime_tools = loop.run_until_complete(fetch_runtime_tools())
        loop.close()
        for item in runtime_tools:
            schema = item.get("schema")
            if schema:
                tools.append(schema)
        return tools
    except Exception as exc:
        logger.warning("Runtime tool list failed, falling back to local: %s", exc)

    tools.extend(_load_local_schemas())
    return tools


async def aload_dynamic_tools() -> list[dict]:
    """Load all tools (forge schemas + runtime tools) asynchronously."""
    tools = [
        build_generate_new_tool_schema(),
        build_propose_tool_batch_schema(),
        build_edit_existing_tool_schema(),
        build_open_skill_app_schema(),
    ]
    runtime_loaded = False
    try:
        runtime_tools = await fetch_runtime_tools()
        for item in runtime_tools:
            schema = item.get("schema")
            if schema:
                tools.append(schema)
                runtime_loaded = True
    except Exception as exc:
        logger.warning("Runtime tool list failed, falling back to local: %s", exc)

    if not runtime_loaded:
        tools.extend(_load_local_schemas())
    return tools


async def alist_tool_summaries() -> list[dict]:
    summaries_by_name: dict[str, dict] = {}
    try:
        runtime_tools = await fetch_runtime_tools()
        for t in runtime_tools:
            name = t.get("name", "")
            if not name:
                continue
            summaries_by_name[name] = {
                "name": name,
                "description": t.get("description", ""),
            }
    except Exception as exc:
        logger.warning("Runtime summaries failed: %s", exc)

    if not summaries_by_name:
        for schema in _load_local_schemas():
            name, description = _schema_name_and_description(schema)
            if name:
                summaries_by_name[name] = {"name": name, "description": description}

    enriched: list[dict] = []
    for name, base in sorted(summaries_by_name.items(), key=lambda item: item[0]):
        enriched.append(
            _summary_from_manifest(name, base.get("description", ""))
        )
    return enriched


async def execute_dynamic_tool(name: str, arguments: dict, *, run_id: str = "") -> str:
    logger.debug("[run=%s][TOOL] executing %s args=%s", run_id or "-", name, arguments)
    return await runtime_run_tool(name, arguments)


async def execute_skill_action(skill_name: str, arguments: dict, *, run_id: str = "") -> dict:
    """Run an interactive skill action and return structured result + fresh skill data."""
    if not is_interactive_skill(skill_name):
        raise ValueError(f"Skill '{skill_name}' is not an interactive skill.")
    manifest = read_tool_manifest(skill_name)
    action = arguments.get("action")
    if not action or not isinstance(action, str):
        raise ValueError("Missing required 'action' parameter.")
    operations = (manifest or {}).get("operations") or []
    if action not in operations:
        raise ValueError(
            f"Action {action!r} is not allowed. Allowed: {sorted(operations)}"
        )
    raw = await execute_dynamic_tool(skill_name, arguments, run_id=run_id)
    parsed: object = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
    return {
        "ok": True,
        "result": parsed,
        "data": read_skill_data(skill_name),
    }


def validate_tool_module(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return "get_tool_schema" in names and "run" in names


def _get_tool_schema_function(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "get_tool_schema":
            return node
    return None


def _function_has_imports(fn: ast.FunctionDef) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
    return False


def _module_has_top_level_imports(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
    return False


def _get_tool_schema_in_isolation(code: str) -> dict:
    tree = ast.parse(code)
    schema_fn = _get_tool_schema_function(tree)
    if schema_fn is None or _function_has_imports(schema_fn):
        raise ValueError("get_tool_schema is missing or contains imports.")
    mod = types.ModuleType("tool_schema_only")
    isolated = ast.Module(body=[schema_fn], type_ignores=[])
    exec(compile(isolated, "<tool_schema_only>", "exec"), mod.__dict__)
    schema = mod.get_tool_schema()
    if not isinstance(schema, dict):
        raise TypeError("get_tool_schema() must return a dict.")
    return schema


def validate_tool_schema(code: str) -> tuple[bool, str]:
    code = sanitize_python_json_literals(code)
    if not validate_tool_module(code):
        return False, "Generated tool code is missing get_tool_schema() or run()."
    try:
        tree = ast.parse(code)
        if _module_has_top_level_imports(tree):
            schema = _get_tool_schema_in_isolation(code)
        else:
            mod = _exec_tool_module_from_source(code, "tool_validation_mod")
            schema = mod.get_tool_schema()
            if not isinstance(schema, dict):
                return False, "get_tool_schema() must return a dict."
        name, _ = _schema_name_and_description(schema)
        if not name:
            return False, "Tool schema is missing a name."
        return True, ""
    except Exception as exc:
        return False, f"get_tool_schema() failed: {exc}"


def read_tool_test(tool_name: str) -> str:
    path = TOOLS_DIR / f"{tool_name}.test.py"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_tool_files(
    tool_name: str,
    code: str,
    requirements: list[str] | None = None,
    test_code: str = "",
    manifest: dict | None = None,
    ui_files: dict[str, str] | None = None,
) -> Path:
    code = sanitize_python_json_literals(code)
    schema_ok, schema_reason = validate_tool_schema(code)
    if not schema_ok:
        raise ValueError(schema_reason)

    target = TOOLS_DIR / f"{tool_name}.py"
    target.write_text(code, encoding="utf-8")

    req_path = TOOLS_DIR / f"{tool_name}.requirements.txt"
    reqs = normalize_requirements(requirements or [])
    if reqs:
        req_path.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    elif req_path.exists():
        req_path.unlink()

    test_path = TOOLS_DIR / f"{tool_name}.test.py"
    if test_code:
        test_path.write_text(test_code, encoding="utf-8")

    manifest_path = TOOLS_DIR / f"{tool_name}.manifest.json"
    if manifest:
        ok, reason = validate_ui_files(ui_files, manifest, tool_name)
        if not ok:
            raise ValueError(reason)
        write_tool_manifest(tool_name, manifest)
        if manifest.get("kind") == "interactive":
            data_path = skill_data_path(tool_name)
            if not data_path.exists():
                write_skill_data(tool_name, dict(DEFAULT_SKILL_DATA))
        write_ui_files(tool_name, ui_files, manifest)
    elif manifest_path.exists():
        manifest_path.unlink()
        remove_ui_files(tool_name)

    return target


def write_tool(tool_name: str, code: str) -> Path:
    return write_tool_files(tool_name, code, [])


def tool_artifact_paths(tool_name: str) -> tuple[list[Path], list[Path]]:
    """Return file and directory paths owned by a tool (including interactive skill data)."""
    _validate_tool_name(tool_name)
    files = [
        TOOLS_DIR / f"{tool_name}.py",
        TOOLS_DIR / f"{tool_name}.requirements.txt",
        TOOLS_DIR / f"{tool_name}.test.py",
        TOOLS_DIR / f"{tool_name}.manifest.json",
        skill_data_path(tool_name),
        TOOLS_DIR / f".verify_{tool_name}_test_run.py",
    ]
    dirs = [STAGING_DIR / tool_name]
    ui_dir = skill_ui_dir(tool_name)
    if ui_dir.exists():
        dirs.append(ui_dir)
    return files, dirs


def _unlink_tool_pycache(tool_name: str) -> None:
    pycache = TOOLS_DIR / "__pycache__"
    if not pycache.is_dir():
        return
    for pattern in (f"{tool_name}.cpython-*.pyc", f"{tool_name}.test.cpython-*.pyc"):
        for cached in pycache.glob(pattern):
            cached.unlink(missing_ok=True)


def _remove_tool_artifacts(tool_name: str) -> None:
    files, dirs = tool_artifact_paths(tool_name)
    for path in files:
        path.unlink(missing_ok=True)
    for path in dirs:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    _unlink_tool_pycache(tool_name)


async def delete_tool_async(tool_name: str) -> None:
    files, dirs = tool_artifact_paths(tool_name)
    if not any(path.exists() for path in files) and not any(path.exists() for path in dirs):
        raise FileNotFoundError(f"Tool '{tool_name}' not found.")

    await runtime_delete_tool(tool_name)
    _remove_tool_artifacts(tool_name)


async def get_new_packages_for_requirements(requirements: list[str]) -> tuple[list[str], list[str]]:
    manifest = await fetch_runtime_manifest()
    approved = manifest.get("approved_packages") or []
    new_packages = diff_new_requirements(requirements, approved)
    return new_packages, approved
