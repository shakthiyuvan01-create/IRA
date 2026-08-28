"""forge_skill — JARVIS-native skill forging (Ada-SI Forge concept).

Given a natural-language capability request, uses the Gemini brain to WRITE a
new action module under actions/, py_compile-test it (one self-fix retry),
then register it with IRA's dynamic tool registry so the brain can call it
immediately. No LiteLLM sidecar, no new API keys — the same key IRA already
uses. Pure Python; safe-by-construction (compiles or it doesn't ship).

The heavier dashboard Forge (dashboard/forge/*, Ada-SI port) remains available
via the remote dashboard when a LiteLLM proxy is configured; this is the
in-voice, self-improving path.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import traceback
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]   # repo root
ACTIONS_DIR  = PROJECT_ROOT / "actions"

_SYSTEM_PROMPT = """\
You are the Forge inside IRA, a PyQt6 desktop AI assistant by Yuvan.
Write a single new Python ACTION MODULE that implements the requested capability.

RULES:
- The module goes in the actions/ directory. Its public entry point MUST be a
  function named exactly:  run(args: dict) -> str
  It receives a JSON-ish dict of arguments and returns a short human-readable
  result string (the assistant repeats this to the user).
- The module must be SELF-CONTAINED: standard library only, no IRA imports, no
  project imports. Import heavy third-party libs lazily inside the function.
- It must be safe and defensive: wrap risky parts in try/except and return a
  clear message instead of raising.
- File name: lowercase snake_case, no spaces.
- Output STRICT JSON only:
  {"name": "...", "description": "one line for the tool registry", "source": "..."}
  where source is the COMPLETE file content.
"""

_MODEL = "gemini-2.5-flash"


def _api_key() -> str:
    try:
        from core.config import get_config
        cfg = get_config()
        return cfg.get("gemini_api_key") or ""
    except Exception:
        return ""


def _ask_gemini(prompt: str, *, fix_note: str = "") -> dict[str, Any]:
    from google import genai
    client = genai.Client(api_key=_api_key())
    sys_inst = _SYSTEM_PROMPT + (f"\n\nPREVIOUS ATTEMPT FAILED. {fix_note}" if fix_note else "")
    resp = client.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=genai.types.GenerateContentConfig(system_instruction=sys_inst),
    )
    text = (resp.text or "").strip()
    # tolerate ```json fences
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict) or not data.get("source"):
        raise ValueError("Forge returned invalid JSON spec")
    return data


def _safe_name(raw: str) -> str:
    name = "".join(c if (c.isalnum() or c == "_") else "_" for c in raw.lower()).strip("_")
    if not name:
        name = "forged_tool"
    if name[0].isdigit():
        name = "f" + name
    return name[:48]


def _validate_and_compile(module_name: str, source: str) -> None:
    """AST + real compile; raises on any problem."""
    ast.parse(source)
    # ensure the run() entry point exists
    tree = ast.parse(source)
    has_run = any(
        isinstance(n, ast.FunctionDef) and n.name == "run"
        for n in ast.walk(tree)
    )
    if not has_run:
        raise ValueError("module has no run(args: dict) -> str entry point")
    compile(source, f"{module_name}.py", "exec")


def forge_skill(request: str, registry: dict[str, Any] | None = None,
                declarations: list[dict[str, Any]] | None = None) -> str:
    """Create a new IRA action from a natural-language request.

    registry:      main.py's self._dynamic_tools (name -> run function) — if
                   provided, the forged module is registered for dispatch.
    declarations:  main.py's TOOL_DECLARATIONS list — the tool entry is
                   appended so the model can see and call it.
    """
    request = (request or "").strip()
    if not request:
        return "Forge needs a capability request."
    if len(request) > 600:
        request = request[:600]

    last_err = ""
    for attempt in range(2):                      # one self-fix retry
        try:
            spec = _ask_gemini(
                f"Create an IRA action for: {request}",
                fix_note=last_err,
            )
            name = _safe_name(spec.get("name", "forged_tool"))
            desc = str(spec.get("description", "")).strip()[:200]
            source = str(spec.get("source", "")).strip()
            _validate_and_compile(name, source)
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt == 1:
                return f"Forge failed twice: {last_err}"
    else:
        return f"Forge failed: {last_err}"

    # ── ship it ──────────────────────────────────────────────────────────
    path = ACTIONS_DIR / f"{name}.py"
    try:
        path.write_text(source, encoding="utf-8")
    except Exception as e:
        return f"Forge could not write {path.name}: {e}"

    # ── register for dispatch ────────────────────────────────────────────
    registered = False
    if registry is not None:
        try:
            import importlib.util
            module_name = f"actions.{name}"
            spec = importlib.util.spec_from_file_location(module_name, str(path))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            registry[name] = lambda args, _m=mod: _m.run(args or {})
            registered = True
        except Exception as e:
            return (f"Forge wrote {path.name} (compiles) but could not register: {e}")

    if declarations is not None:
        declarations.append({
            "name": name,
            "description": desc or f"Forged action for: {request}",
            "parameters": {"type": "OBJECT", "properties": {}, "required": []},
        })

    status = "registered and live" if registered else "written (registration skipped)"
    return f"Forged new skill '{name}' — {status}. File: actions/{name}.py"


def list_forged() -> list[str]:
    """Names of runtime-forged skills present in actions/."""
    if not ACTIONS_DIR.exists():
        return []
    return sorted(p.stem for p in ACTIONS_DIR.glob("*.py")
                  if not p.name.startswith("__"))
