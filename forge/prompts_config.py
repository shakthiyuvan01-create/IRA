"""Loadable prompt configuration for IRA Forge (ported from Ada-SI)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from forge.forge_routing import ForgeCodegenProfile, ForgeReviseProfile, infer_codegen_profile, infer_revise_profile

# --- Forge shared appendix defaults ---

_DEFAULT_FORGE_RUNTIME_CONTEXT = """Runtime context (always true):
- Forged Python tools execute locally on the user's machine in a dedicated tool runtime (Python 3.12 venv).
- Tools can access the local filesystem under custom_tools/ and pip packages installed in the tool runtime.
- TTS and audio tools are supported (e.g. gTTS, pyttsx3): generate speech/audio files, save under custom_tools/, and return file paths."""

# --- Forge phase prompt defaults ---

_DEFAULT_FORGE_PLAN_PROMPT = """You are an expert Python tool architect for a self-improving AI agent.

The user needs a new callable tool. Produce a clear implementation plan in markdown with these sections:
## Skill Kind and UI
Decide headless vs interactive. For interactive skills, always use template: custom with a custom HTML/CSS/JS iframe UI (ui_files). Define operations freely (any names matching run(action=...)). Map each UI button or intent to an operation in manifest.ui.actions — action keys may be any names (e.g. start, pause, reset or fetch, create, delete). Describe the UI layout, state shape, and persistence (typically skill_data JSON under Path(__file__).parent / "skill_data" / "{tool_name}.json"; use {"records": [...]} or a single state object in records[0] as appropriate).
## Architecture Changes
## Function Schema
Use a single tool with an `action` enum parameter for interactive skills (e.g. get_state, start, pause, reset — or list_items, add_item, delete_item).
## Execution Steps
Interactive skills persist data to Path(__file__).parent / "skill_data" / "{tool_name}.json" when persistence is needed.
## Risks and Limitations

Be specific about inputs, outputs, and edge cases. Do not write full Python code yet."""

_DEFAULT_FORGE_REVISE_PLAN_PROMPT = """You are an expert Python tool architect for a self-improving AI agent.

The user rejected a proposed tool plan and requested revisions. Produce an updated implementation plan in markdown with these sections:
## Architecture Changes
## Function Schema
## Execution Steps
## Risks and Limitations

Incorporate the user's requested changes while keeping the plan focused and implementable. Do not write full Python code yet."""

_DEFAULT_FORGE_EDIT_PLAN_PROMPT = """You are an expert Python tool architect for a self-improving AI agent.

The user wants to modify an EXISTING installed tool. Produce an updated implementation plan in markdown with these sections:
## Architecture Changes
## Function Schema
## Execution Steps
## Risks and Limitations

Reference the current tool behavior and describe only what must change. Do not write full Python code yet."""

_COMMON_CODEGEN_RULES = """Rules:
- requirements: list every PyPI package the tool needs at runtime (e.g. httpx, pyttsx3). Use [] for stdlib-only tools.
- Do NOT include pip, setuptools, or wheel in requirements.
- tool_name must match the module filename stem
- Do NOT import third-party packages at module top level; lazy-import inside run() only. get_tool_schema() must be import-free.
- Verification runs in a temporary local venv under staging/ with requirements auto-installed before tests
- test_code loads the tool from /workspace/{tool_name}.py (paths are rewritten to the verify workspace at runtime)
- test_run.py must exit 0 on success
- Keep tools minimal and focused
- In tool_code Python source use Python literals True, False, and None — never JSON true, false, or null
- JSON string values MUST be valid JSON: escape every double quote inside code as \\", newlines as \\n, backslashes as \\\\
- ALL file paths in run() return values MUST use the /workspace/ prefix (never /app/custom_tools/)
- test_code MUST use this exact load pattern:

import importlib.util

def load_tool():
    spec = importlib.util.spec_from_file_location(
        "tool_mod", "/workspace/{tool_name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod"""

_DEFAULT_FORGE_CODE_HEADLESS_PROMPT = """You are an expert Python developer building HEADLESS tools for a self-improving AI agent.

Each tool module MUST define exactly:
1. get_tool_schema() -> dict  (OpenAI-compatible function schema)
2. run(**kwargs) -> str or JSON-serializable value

This is a HEADLESS tool — set "manifest": null and omit ui_files (or use empty ui_files {{}}).

Respond with ONLY valid JSON (no markdown fences) in this shape:
{{
  "tool_code": "<full Python module source>",
  "test_code": "<test_run.py>",
  "requirements": [],
  "manifest": null,
  "ui_files": {{}}
}}

Headless test rules:
- Mock network, subprocess, or TTS engine calls with unittest.mock.patch when live I/O is undesirable
- Do NOT use interactive skill_data persistence patterns

""" + _COMMON_CODEGEN_RULES

_DEFAULT_FORGE_CODE_INTERACTIVE_BUILTIN_PROMPT = """You are an expert Python developer building INTERACTIVE skills with LEGACY built-in UI templates (list, calendar, or table).

Use this profile ONLY when editing an existing skill that already uses template list, calendar, or table. New interactive skills should use the custom iframe prompt instead.

Each tool module MUST define exactly:
1. get_tool_schema() -> dict  (OpenAI-compatible function schema)
2. run(**kwargs) -> str or JSON-serializable value

The React app renders the popup UI — do NOT emit ui_files. Set manifest only.

Respond with ONLY valid JSON (no markdown fences) in this shape:
{{
  "tool_code": "<full Python module source>",
  "test_code": "<test_run.py>",
  "requirements": [],
  "manifest": {{
    "kind": "interactive",
    "display_name": "Human Name",
    "icon": "list",
    "operations": ["list_tasks", "add_task", "delete_task"],
    "ui": {{
      "template": "list",
      "title_field": "title",
      "done_field": "done",
      "actions": {{
        "fetch": "list_tasks",
        "create": "add_task",
        "delete": "delete_task",
        "toggle": "complete_task"
      }}
    }}
  }},
  "ui_files": {{}}
}}

UI templates: calendar (scheduling), list (todos), table (generic CRUD).

manifest.ui.actions (required):
- fetch: optional list action
- create: add record
- delete: remove by id — params: task_id (list), event_id (calendar), id (table)
- toggle: list only — params: task_id

Interactive tool rules:
- Persist data to Path(__file__).parent / "skill_data" / "{tool_name}.json"
- Store shape: {{"records": [{{"id": "...", ...fields...}}]}}
- Use run(action=...) with action enum matching manifest.operations
- Interactive skill tests: call real run() CRUD — /workspace/skill_data/ is writable and pre-seeded with {{"records": []}}

""" + _COMMON_CODEGEN_RULES

_CUSTOM_APP_JS_SCAFFOLD = r'''AdaSkill.init();

async function refresh() {
  // Optional: load persisted state via AdaSkill.getData()
  const data = await AdaSkill.getData();
  const records = data.records || [];
  // TODO: render state into the DOM
}

document.getElementById('action-btn').addEventListener('click', async () => {
  await AdaSkill.call('OPERATION_NAME', { /* params */ });
  await refresh();
});

AdaSkill.onDataChanged(refresh);
AdaSkill.loadActionsFromTools().finally(refresh);'''

_DEFAULT_FORGE_CODE_INTERACTIVE_CUSTOM_PROMPT = f"""You are an expert Python developer building INTERACTIVE skills with a CUSTOM iframe UI (HTML/CSS/JS).

Each tool module MUST define exactly:
1. get_tool_schema() -> dict  (OpenAI-compatible function schema)
2. run(**kwargs) -> str or JSON-serializable value

Respond with ONLY valid JSON (no markdown fences) in this shape:
{{{{
  "tool_code": "<full Python module source>",
  "test_code": "<test_run.py>",
  "requirements": [],
  "manifest": {{{{
    "kind": "interactive",
    "display_name": "Human Name",
    "icon": "file-text",
    "operations": ["get_state", "start", "pause", "reset"],
    "ui": {{{{
      "template": "custom",
      "entry": "index.html",
      "actions": {{{{
        "getState": "get_state",
        "start": "start",
        "pause": "pause",
        "reset": "reset"
      }}}}
    }}}}
  }}}},
  "ui_files": {{{{
    "index.html": "<!DOCTYPE html>... loads /static/skill-sdk.js and app.js",
    "app.js": "...",
    "styles.css": "..."
  }}}}
}}}}

manifest.ui.actions:
- Keys may be ANY names (UI intent labels, e.g. getState, start, addItem, rollDice)
- Values MUST be operation names listed in manifest.operations
- Map each button/control to the operation it triggers

Examples (all valid):
- Stopwatch: operations get_state, start, pause, reset — actions {{getState, start, pause, reset}}
- Todo app: operations list_items, add_item, delete_item — actions {{fetch: list_items, create: add_item, delete: delete_item}}
- Game: operations get_board, make_move, reset_game — actions {{load: get_board, move: make_move, reset: reset_game}}

Custom UI rules:
- No external CDN scripts; index.html MUST load /static/skill-sdk.js
- Use AdaSkill singleton — do NOT use `new AdaSkill()`
- AdaSkill.init() once at startup
- AdaSkill.call('operation_name', {{{{ param: value }}}}) — first arg is action string, second is params
- AdaSkill.getData() returns {{{{ records: [...] }}}} when persistence uses records — optional for stateful UIs
- Refresh UI after mutations via getData(), onDataChanged, or local optimistic state

app.js scaffold (fill in DOM rendering and operation names):
```
{_CUSTOM_APP_JS_SCAFFOLD}
```

Interactive tool rules:
- Persist data to Path(__file__).parent / "skill_data" / "{{tool_name}}.json" when needed
- Use any JSON shape appropriate (typically {{{{"records": [...]}}}} or a single state object in records[0])
- manifest.operations must match run(action=...) enum
- test_code exercises each operation via run(action=...)

""" + _COMMON_CODEGEN_RULES

_COMMON_EDIT_RULES = """Rules:
- Preserve tool_name as the module filename stem
- In tool_code Python source use Python literals True, False, and None — never JSON true, false, or null
- requirements: full list of PyPI packages needed after your edit (not just new ones)
- test_code loads the tool from /workspace/{tool_name}.py
- ALL file paths in run() return values use /workspace/ prefix
- JSON string values MUST be valid JSON with escaped quotes and newlines"""

_DEFAULT_FORGE_EDIT_CODE_HEADLESS_PROMPT = """You are an expert Python developer updating an existing HEADLESS tool.

Respond with ONLY valid JSON (no markdown fences):
{{
  "tool_code": "<full updated Python module source>",
  "test_code": "<updated test_run.py>",
  "requirements": [],
  "manifest": null,
  "ui_files": {{}}
}}

Set manifest null. Mock network/subprocess in test_code.

""" + _COMMON_EDIT_RULES

_DEFAULT_FORGE_EDIT_CODE_INTERACTIVE_BUILTIN_PROMPT = """You are an expert Python developer updating an INTERACTIVE skill with built-in UI (list, calendar, or table).

Do NOT emit ui_files — React renders the popup. Update manifest when needed.

Respond with ONLY valid JSON (no markdown fences):
{{
  "tool_code": "...",
  "test_code": "...",
  "requirements": [],
  "manifest": {{ "kind": "interactive", "ui": {{ "template": "list", "actions": {{...}} }}, "operations": [...] }},
  "ui_files": {{}}
}}

Interactive tests use real /workspace/skill_data/ CRUD.

""" + _COMMON_EDIT_RULES

_DEFAULT_FORGE_EDIT_CODE_INTERACTIVE_CUSTOM_PROMPT = """You are an expert Python developer updating an INTERACTIVE skill with CUSTOM iframe UI.

Include ui_files when template is custom. index.html must load /static/skill-sdk.js.
Use AdaSkill.init(), AdaSkill.call('action', {{params}}), AdaSkill.getData() — never `new AdaSkill()`.

Respond with ONLY valid JSON (no markdown fences):
{{
  "tool_code": "...",
  "test_code": "...",
  "requirements": [],
  "manifest": {{ "kind": "interactive", "ui": {{ "template": "custom", "entry": "index.html", "actions": {{...}} }} }},
  "ui_files": {{ "index.html": "...", "app.js": "...", "styles.css": "..." }}
}}

""" + _COMMON_EDIT_RULES

_DEFAULT_FORGE_PREVIEW_REVIEW_PROMPT = """You review an interactive skill before it is shown to the user in a popup preview.

Check tool_code, test_code, manifest, and ui_files (when template is custom) for correctness.

Respond with ONLY valid JSON (no markdown fences):
{{ "ok": true }} OR {{ "ok": false, "issues": ["issue 1", "issue 2"] }}

Review checklist:
- manifest.kind is "interactive"
- manifest.operations match run(action=...) enum in tool_code
- manifest.ui.actions: any key names allowed; each value must be in manifest.operations
- For custom template: index.html loads /static/skill-sdk.js; app.js uses AdaSkill.init() not new AdaSkill()
- AdaSkill.call uses string action as first arg; getData() is optional
- For legacy built-in templates (list/calendar/table): ui_files must be empty
- Button/form wiring in custom app.js is plausible (handlers attached, not dead code)"""

_DEFAULT_FORGE_FIX_PREVIEW_PROMPT = """You fix an interactive skill that failed automated preview QA (static UI lint, API contract test, or review issues).

Given the issues list, return corrected artifacts.

Respond with ONLY valid JSON (no markdown fences):
{{ "tool_code": "...", "test_code": "...", "manifest": {{ ... }}, "ui_files": {{ ... }} }}

Rules:
- Preserve tool_name as the module filename stem
- Fix every listed issue
- manifest.kind must remain "interactive"
- For custom template: preserve arbitrary manifest.ui.actions keys; fix app.js SDK usage; include full ui_files
- For legacy built-in templates: ui_files must be {{}}
- test_code loads from /workspace/{tool_name}.py; use Python True/False/None
- ALL file paths in run() return values use /workspace/ prefix"""

_DEFAULT_FORGE_REVISE_PREVIEW_BUILTIN_PROMPT = """You revise an interactive skill preview (list, calendar, or table template) based on user UI feedback.

The built-in React app renders the UI — fix tool_code, test_code, and manifest. Do NOT return ui_files.

When a screenshot is attached, use it to spot layout issues, wrong fields, and empty states.

Respond with ONLY valid JSON (no markdown fences):
{{ "tool_code": "...", "test_code": "...", "manifest": {{ ... }}, "ui_files": {{}} }}

Rules:
- Preserve tool_name as the module filename stem
- manifest.kind must remain "interactive"
- Align manifest.ui with record shape in tool_code
- manifest.operations must match run(action=...) enum values
- manifest.ui.actions must map create/delete/toggle to operations
- Persist to Path(__file__).parent / "skill_data" / "{tool_name}.json"
- In tool_code use Python True, False, None — never JSON true/false/null
- ALL file paths in run() return values use /workspace/ prefix"""

_DEFAULT_FORGE_REVISE_PREVIEW_CUSTOM_PROMPT = """You revise an interactive skill preview with CUSTOM iframe UI based on user UI feedback.

Fix tool_code, test_code, manifest, and ui_files so the UI and run(action=...) behavior match.

When a screenshot is attached, use it to spot layout issues, broken buttons, SDK misuse, and empty states.

Respond with ONLY valid JSON (no markdown fences):
{{ "tool_code": "...", "test_code": "...", "manifest": {{ ... }}, "ui_files": {{ ... }} }}

Custom UI SDK checklist:
- index.html loads /static/skill-sdk.js
- AdaSkill.init() — do NOT use `new AdaSkill()`
- AdaSkill.call('operation', {{ params }}) — first arg is string action name
- manifest.ui.actions keys may be any names; values must match manifest.operations
- Wire click handlers to buttons; refresh via getData(), onDataChanged, or local state
- Handle errors with console.error or alert

Rules:
- Preserve tool_name as the module filename stem
- manifest.kind must remain "interactive"; template must stay "custom"
- Preserve manifest.ui.actions key names unless the user asks to change them
- manifest.operations must match run(action=...) enum values
- Persist to Path(__file__).parent / "skill_data" / "{tool_name}.json" when needed
- In tool_code use Python True, False, None — never JSON true/false/null
- ALL file paths in run() return values use /workspace/ prefix"""

_DEFAULT_FORGE_FIX_TEST_PROMPT = """You are an expert Python test engineer fixing verification test failures.

The tool module (tool_code) is correct — only fix test_code so it passes in a temporary
local venv with the tool at /workspace/{tool_name}.py and /workspace/skill_data/ writable
with pre-seeded {{"records": []}} for interactive skills. Requirements are installed in the verify venv.

Respond with ONLY valid JSON (no markdown fences):
{{ "test_code": "<fixed test_run.py source>" }}

Rules:
- Use importlib.util.spec_from_file_location to load the tool from /workspace/{tool_name}.py
- Mock network, subprocess, or TTS engine calls with unittest.mock.patch when live I/O is undesirable
- Interactive skills: do NOT mock skill_data persistence — test real run() CRUD against /workspace/skill_data/
- Assert paths using /workspace/ prefix when checking run() return values (never host paths like C:/...)
- test_run.py must exit 0 when run as: python test_run.py from the verify workspace directory"""

_DEFAULT_FORGE_FIX_CODEGEN_PROMPT = """You repair malformed tool-creator JSON responses.

Return ONLY valid JSON (no markdown fences):
{{ "tool_code": "...", "test_code": "...", "requirements": [], "manifest": null, "ui_files": {{}} }}

Rules:
- tool_code must define get_tool_schema() and run()
- get_tool_schema() must use Python True, False, and None — never JSON true, false, or null
- test_code loads tool from /workspace/{tool_name}.py via importlib
- requirements is a list of PyPI package strings (or [])
- ALL file paths in run() returns use /workspace/ prefix
- Escape quotes and newlines properly inside JSON strings"""

_DEFAULT_FORGE_FIX_VALIDATION_PROMPT = """You fix Python tool modules that failed static validation.

Return ONLY valid JSON (no markdown fences):
{{ "tool_code": "...", "test_code": "..." }}

Rules:
- tool_code MUST define get_tool_schema() and run()
- get_tool_schema() must use Python True, False, and None — never JSON true, false, or null
- test_code MUST load via importlib from /workspace/{tool_name}.py and include tests/mocks
- ALL file paths in run() return values use /workspace/ prefix
- Fix only what validation requires; keep behavior from the plan"""

_DEFAULT_FORGE_FIX_RUNTIME_PROMPT = """You fix tool_code and/or test_code failures in the persistent tool runtime.

Tests run with cwd=custom_tools. Load tools via importlib from the tool module path.
Use /workspace/ prefix in run() return values for consistency with verification tests.

Respond with ONLY valid JSON (no markdown fences):
{{ "tool_code": "...", "test_code": "..." }}

Rules:
- Fix path mismatches: use /workspace/ in run() return values AND test assertions
- Mock network/subprocess in test_code; runtime has network but tests must not call live APIs unless intended
- tool_code must keep get_tool_schema() and run(); lazy-import third-party deps inside run() only"""

_DEFAULT_FORGE_GOOGLE_SEARCH_GUIDANCE = """

Google Search grounding is ENABLED for this Forge request. Use it proactively — do not guess API or library details.

When planning or writing tool_code, search when you need:
- Third-party API docs: base URLs, paths, auth (API keys, OAuth, headers), request/response JSON shape, error codes, rate limits
- PyPI package names, import paths, and current usage (verify the package exists and matches the API you implement)
- Breaking changes, deprecations, or version-specific behavior for libraries listed in requirements
- SDK method names, query parameters, or response fields you are uncertain about

Workflow: search first when unsure, then implement tool_code, requirements, manifest, and tests grounded in what you find. Prefer official documentation. Do not invent endpoints, field names, or package APIs."""

# --- Tool schema descriptions ---

_DEFAULT_TOOL_GENERATE_NEW_DESCRIPTION = (
    "Request creation of a new Python tool when the user needs a capability you do "
    "not have installed: live/real-time data (weather, markets, news), external APIs, "
    "web fetching, persistence, filesystem access, TTS/audio (gTTS, pyttsx3), or custom automation. "
    "Call this instead of asking the user for details you could specify in description. "
    "Do not use for pure chat or static facts answerable without tools or APIs."
)

_DEFAULT_TOOL_EDIT_EXISTING_DESCRIPTION = (
    "Modify an installed tool when the user wants to fix bugs, change behavior, "
    "add inputs/outputs, or update dependencies. Use when a tool exists but needs "
    "changes — not for creating a brand-new capability under a new name."
)

_DEFAULT_TOOL_PROPOSE_BATCH_DESCRIPTION = (
    "Propose creating 2–10 new independent Python tools at once when the user needs "
    "multiple separate capabilities (e.g. weather lookup AND stock prices). The user "
    "will confirm before any forging begins. Do not use for a single tool (use "
    "generate_new_tool), a single app with multiple features (use one interactive "
    "generate_new_tool), or editing existing tools."
)

PROMPT_KEYS = (
    "forge_runtime_context",
    "forge_plan_prompt",
    "forge_revise_plan_prompt",
    "forge_edit_plan_prompt",
    "forge_code_headless_prompt",
    "forge_code_interactive_builtin_prompt",
    "forge_code_interactive_custom_prompt",
    "forge_edit_code_headless_prompt",
    "forge_edit_code_interactive_builtin_prompt",
    "forge_edit_code_interactive_custom_prompt",
    "forge_preview_review_prompt",
    "forge_fix_preview_prompt",
    "forge_revise_preview_builtin_prompt",
    "forge_revise_preview_custom_prompt",
    "forge_fix_test_prompt",
    "forge_fix_codegen_prompt",
    "forge_fix_validation_prompt",
    "forge_fix_runtime_prompt",
    "tool_generate_new_description",
    "tool_edit_existing_description",
    "tool_propose_batch_description",
)

BASE_DIR = Path(__file__).resolve().parent.parent
STAGING_DIR = BASE_DIR / "staging"
CONFIG_PATH = STAGING_DIR / "prompts_config.json"
PROMPTS_DEFAULTS_REVISION = 8
CONFIG_REVISION_KEY = "_defaults_revision"


class PromptsConfig(TypedDict):
    forge_runtime_context: str
    forge_plan_prompt: str
    forge_revise_plan_prompt: str
    forge_edit_plan_prompt: str
    forge_code_headless_prompt: str
    forge_code_interactive_builtin_prompt: str
    forge_code_interactive_custom_prompt: str
    forge_edit_code_headless_prompt: str
    forge_edit_code_interactive_builtin_prompt: str
    forge_edit_code_interactive_custom_prompt: str
    forge_preview_review_prompt: str
    forge_fix_preview_prompt: str
    forge_revise_preview_builtin_prompt: str
    forge_revise_preview_custom_prompt: str
    forge_fix_test_prompt: str
    forge_fix_codegen_prompt: str
    forge_fix_validation_prompt: str
    forge_fix_runtime_prompt: str
    tool_generate_new_description: str
    tool_edit_existing_description: str
    tool_propose_batch_description: str


_CODE_PROMPT_KEY: dict[ForgeCodegenProfile, str] = {
    "headless": "forge_code_headless_prompt",
    "interactive_builtin": "forge_code_interactive_builtin_prompt",
    "interactive_custom": "forge_code_interactive_custom_prompt",
}

_EDIT_CODE_PROMPT_KEY: dict[ForgeCodegenProfile, str] = {
    "headless": "forge_edit_code_headless_prompt",
    "interactive_builtin": "forge_edit_code_interactive_builtin_prompt",
    "interactive_custom": "forge_edit_code_interactive_custom_prompt",
}

_REVISE_PREVIEW_PROMPT_KEY: dict[ForgeReviseProfile, str] = {
    "interactive_builtin": "forge_revise_preview_builtin_prompt",
    "interactive_custom": "forge_revise_preview_custom_prompt",
}


_cache: PromptsConfig | None = None


def _apply_stale_prompt_replacements(config: PromptsConfig) -> tuple[PromptsConfig, bool]:
    """Replace saved prompts that still contain retired default text."""
    defaults = default_prompts_config()
    stale_markers = (
        "Do not create tools for microphone",
        "Tools cannot access local user hardware",
        "or requests involving microphone, camera, speakers",
        "voice/TTS, screen capture",
        "Do not use libraries that require microphones",
        "other local hardware access",
        "Politely explain these capabilities are not supported",
        "headless Docker container",
        "python:3.12-slim container with NO network",
        "Sandbox has NO network access",
        "fixing sandbox test failures",
        "runs in sandbox at /workspace",
        "UI templates: calendar (scheduling), list (todos/notes), table (generic CRUD).",
        "Use calendar, list, or table templates when they fit CRUD record apps",
        "ui.freeform: true",
        "CRUD custom app (notes, file browser)",
        "Freeform custom app (stopwatch",
        "When manifest.ui.freeform is NOT true",
        "For non-CRUD control/state apps (stopwatch",
        "For non-CRUD apps (stopwatch, counter, timer, game, control panel)",
        "For headless-only tools, set \"manifest\": null or {\"kind\": \"headless\"}.",
        "You are an expert Python developer building tools for a self-improving AI agent.",
        "Include manifest when the skill is interactive (see forge code prompt for manifest shape).",
        "You revise an interactive skill preview based on user UI feedback.",
    )
    updated = dict(config)
    changed = False
    for key in PROMPT_KEYS:
        value = updated.get(key, "")
        if not isinstance(value, str):
            continue
        if any(marker in value for marker in stale_markers):
            updated[key] = defaults[key]  # type: ignore[literal-required]
            changed = True
    return updated, changed  # type: ignore[return-value]


def _migrate_stale_prompts(config: PromptsConfig) -> PromptsConfig:
    updated, changed = _apply_stale_prompt_replacements(config)
    if changed:
        return save_prompts_config(updated)
    return config


def reset_prompts_config() -> PromptsConfig:
    """Reload prompt defaults from source and persist them."""
    global _cache
    import importlib

    mod = importlib.reload(importlib.import_module(__name__))
    _cache = None
    return mod.save_prompts_config(mod.default_prompts_config())


def default_prompts_config() -> PromptsConfig:
    return {
        "forge_runtime_context": _DEFAULT_FORGE_RUNTIME_CONTEXT,
        "forge_plan_prompt": _DEFAULT_FORGE_PLAN_PROMPT,
        "forge_revise_plan_prompt": _DEFAULT_FORGE_REVISE_PLAN_PROMPT,
        "forge_edit_plan_prompt": _DEFAULT_FORGE_EDIT_PLAN_PROMPT,
        "forge_code_headless_prompt": _DEFAULT_FORGE_CODE_HEADLESS_PROMPT,
        "forge_code_interactive_builtin_prompt": _DEFAULT_FORGE_CODE_INTERACTIVE_BUILTIN_PROMPT,
        "forge_code_interactive_custom_prompt": _DEFAULT_FORGE_CODE_INTERACTIVE_CUSTOM_PROMPT,
        "forge_edit_code_headless_prompt": _DEFAULT_FORGE_EDIT_CODE_HEADLESS_PROMPT,
        "forge_edit_code_interactive_builtin_prompt": _DEFAULT_FORGE_EDIT_CODE_INTERACTIVE_BUILTIN_PROMPT,
        "forge_edit_code_interactive_custom_prompt": _DEFAULT_FORGE_EDIT_CODE_INTERACTIVE_CUSTOM_PROMPT,
        "forge_preview_review_prompt": _DEFAULT_FORGE_PREVIEW_REVIEW_PROMPT,
        "forge_fix_preview_prompt": _DEFAULT_FORGE_FIX_PREVIEW_PROMPT,
        "forge_revise_preview_builtin_prompt": _DEFAULT_FORGE_REVISE_PREVIEW_BUILTIN_PROMPT,
        "forge_revise_preview_custom_prompt": _DEFAULT_FORGE_REVISE_PREVIEW_CUSTOM_PROMPT,
        "forge_fix_test_prompt": _DEFAULT_FORGE_FIX_TEST_PROMPT,
        "forge_fix_codegen_prompt": _DEFAULT_FORGE_FIX_CODEGEN_PROMPT,
        "forge_fix_validation_prompt": _DEFAULT_FORGE_FIX_VALIDATION_PROMPT,
        "forge_fix_runtime_prompt": _DEFAULT_FORGE_FIX_RUNTIME_PROMPT,
        "tool_generate_new_description": _DEFAULT_TOOL_GENERATE_NEW_DESCRIPTION,
        "tool_edit_existing_description": _DEFAULT_TOOL_EDIT_EXISTING_DESCRIPTION,
        "tool_propose_batch_description": _DEFAULT_TOOL_PROPOSE_BATCH_DESCRIPTION,
    }


def _normalize_config(data: dict) -> PromptsConfig:
    defaults = default_prompts_config()
    normalized = dict(defaults)
    for key in PROMPT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    return normalized  # type: ignore[return-value]


def load_prompts_config(*, refresh: bool = False) -> PromptsConfig:
    global _cache
    if _cache is not None and not refresh:
        return _cache

    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                revision = raw.get(CONFIG_REVISION_KEY, 0)
                if not isinstance(revision, int):
                    revision = 0
                if revision < PROMPTS_DEFAULTS_REVISION:
                    _cache = save_prompts_config(default_prompts_config())
                    return _cache
                _cache = _migrate_stale_prompts(_normalize_config(raw))
                return _cache
        except (OSError, json.JSONDecodeError):
            pass

    _cache = save_prompts_config(default_prompts_config())
    return _cache


def save_prompts_config(data: dict) -> PromptsConfig:
    global _cache
    merged = _normalize_config(data)
    merged, _ = _apply_stale_prompt_replacements(merged)
    STAGING_DIR.mkdir(exist_ok=True)
    payload = {CONFIG_REVISION_KEY: PROMPTS_DEFAULTS_REVISION, **merged}
    CONFIG_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _cache = merged
    return merged


def get_forge_appendix() -> str:
    return load_prompts_config()["forge_runtime_context"].strip()


def _with_forge_appendix(base: str) -> str:
    appendix = get_forge_appendix()
    base = base.strip()
    if not appendix:
        return base
    if not base:
        return appendix
    return f"{base}\n\n{appendix}"


def get_forge_runtime_context() -> str:
    """Alias for get_forge_appendix() — returns the forge runtime context string."""
    return get_forge_appendix()


def get_forge_plan_prompt() -> str:
    return _with_forge_appendix(load_prompts_config()["forge_plan_prompt"])


def get_forge_revise_plan_prompt() -> str:
    return _with_forge_appendix(load_prompts_config()["forge_revise_plan_prompt"])


def get_forge_edit_plan_prompt() -> str:
    return _with_forge_appendix(load_prompts_config()["forge_edit_plan_prompt"])


def get_forge_code_prompt_for_profile(profile: ForgeCodegenProfile) -> str:
    key = _CODE_PROMPT_KEY[profile]
    return _with_forge_appendix(load_prompts_config()[key])  # type: ignore[literal-required]


def get_forge_edit_code_prompt_for_profile(profile: ForgeCodegenProfile) -> str:
    key = _EDIT_CODE_PROMPT_KEY[profile]
    return _with_forge_appendix(load_prompts_config()[key])  # type: ignore[literal-required]


def get_forge_preview_review_prompt() -> str:
    return _with_forge_appendix(load_prompts_config()["forge_preview_review_prompt"])


def get_forge_fix_preview_prompt() -> str:
    return _with_forge_appendix(load_prompts_config()["forge_fix_preview_prompt"])


def get_forge_revise_preview_prompt_for_profile(profile: ForgeReviseProfile) -> str:
    key = _REVISE_PREVIEW_PROMPT_KEY[profile]
    return _with_forge_appendix(load_prompts_config()[key])  # type: ignore[literal-required]


def get_forge_fix_test_prompt() -> str:
    return load_prompts_config()["forge_fix_test_prompt"]


def get_forge_fix_codegen_prompt() -> str:
    return load_prompts_config()["forge_fix_codegen_prompt"]


def get_forge_fix_validation_prompt() -> str:
    return load_prompts_config()["forge_fix_validation_prompt"]


def get_forge_fix_runtime_prompt() -> str:
    return load_prompts_config()["forge_fix_runtime_prompt"]


def get_forge_google_search_guidance() -> str:
    return _DEFAULT_FORGE_GOOGLE_SEARCH_GUIDANCE


def get_tool_generate_new_description() -> str:
    return load_prompts_config()["tool_generate_new_description"]


def get_tool_edit_existing_description() -> str:
    return load_prompts_config()["tool_edit_existing_description"]


def get_tool_propose_batch_description() -> str:
    return load_prompts_config()["tool_propose_batch_description"]


def prompts_config_response() -> dict:
    return {
        "prompts": load_prompts_config(),
    }
