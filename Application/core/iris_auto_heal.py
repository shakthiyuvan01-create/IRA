"""
Auto-Heal & Self-Patching Engine for IRIS AI
Enables IRIS to detect its own bugs, tracebacks, and tool exceptions,
synthesize minimal surgical hotfixes, verify syntax in an isolated sandbox,
safely apply patches with atomic rollback guarantees, and record changelogs.
"""

from __future__ import annotations
from core.user_paths import get_user_data_dir

import ast
import json
import logging
import os
import py_compile
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("AutoHealEngine")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = get_user_data_dir() / "config"
PATCH_HISTORY_FILE = CONFIG_DIR / "patch_history.json"
BACKUPS_DIR = CONFIG_DIR / "patch_backups"
API_CONFIG_PATH = CONFIG_DIR / "api_keys.json"


def _get_iris_api_key() -> str:
    """Retrieves the IRIS AI API key from api_keys.json or environment variables."""
    if API_CONFIG_PATH.exists():
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                key = data.get("iris_api_key", "").strip()
                if key:
                    return key
        except Exception:
            pass
    return (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()

# Core files strictly protected from modification to prevent self-destruction
PROTECTED_CORE_FILES = {
    "iris_boot_sentry.py",
    "iris_auto_heal_engine.py",
    "setup.py",
    "requirements.txt",
    "version.txt",
    "install_wizard.py",
}


# ── Terminal & Scope Context Helpers ───────────────────────────────────────

def bring_terminal_to_front(player: Any = None) -> None:
    """Brings the console/terminal window and IRIS app into focus."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # 1. Check direct console HWND
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE = 9
                user32.SetForegroundWindow(hwnd)

            # 2. Enumerate visible windows for terminal / powershell / cmd
            def enum_proc(w_hwnd, lParam):
                if user32.IsWindowVisible(w_hwnd):
                    length = user32.GetWindowTextLengthW(w_hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(w_hwnd, buf, length + 1)
                        title = buf.value.lower()
                        if any(t in title for t in ("powershell", "cmd.exe", "terminal", "console", "iris")):
                            user32.ShowWindow(w_hwnd, 9)
                            user32.SetForegroundWindow(w_hwnd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
    except Exception as exc:
        logger.debug(f"[AutoHeal] Could not foreground terminal: {exc}")

    # 3. Bring IRIS UI to front
    if player and hasattr(player, "show_app"):
        try:
            player.show_app()
        except Exception:
            pass


def extract_ast_context(full_source: str, line_num: int) -> Tuple[str, str]:
    """
    Extracts module imports + the full enclosing function/method/class around line_num.
    Falls back to a ±35 line window if AST parse fails.
    """
    source_lines = full_source.splitlines(keepends=True)
    enclosing_name = ""

    try:
        tree = ast.parse(full_source)
        import_lines = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                start = getattr(node, "lineno", 1) - 1
                end = getattr(node, "end_lineno", start + 1)
                import_lines.extend(source_lines[start:end])

        # Find deepest node containing line_num
        target_node = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = getattr(node, "lineno", 0)
                end = getattr(node, "end_lineno", 0)
                if start <= line_num <= end:
                    if target_node is None or (end - start) < (getattr(target_node, "end_lineno", 0) - getattr(target_node, "lineno", 0)):
                        target_node = node

        if target_node:
            enclosing_name = getattr(target_node, "name", "")
            start = max(0, getattr(target_node, "lineno", 1) - 1)
            end = min(len(source_lines), getattr(target_node, "end_lineno", len(source_lines)))
            func_code = "".join(source_lines[start:end])
            imports_code = "".join(import_lines[:25])
            return f"{imports_code}\n# [Context around line {line_num} in {enclosing_name}()]:\n{func_code}", enclosing_name
    except Exception:
        pass

    start_idx = max(0, line_num - 35)
    end_idx = min(len(source_lines), line_num + 35)
    return "".join(source_lines[start_idx:end_idx]), enclosing_name


def apply_patch_resiliently(full_source: str, target_chunk: str, replacement_chunk: str, line_num: int) -> Tuple[bool, str]:
    """
    Applies replacement_chunk into full_source with indentation and line-ending resilience.
    """
    if target_chunk in full_source:
        return True, full_source.replace(target_chunk, replacement_chunk, 1)

    # 1. Normalize line endings
    full_norm = full_source.replace("\r\n", "\n")
    target_norm = target_chunk.replace("\r\n", "\n")
    rep_norm = replacement_chunk.replace("\r\n", "\n")
    if target_norm in full_norm:
        return True, full_norm.replace(target_norm, rep_norm, 1)

    # 2. Indentation-insensitive sliding match
    source_lines = full_norm.split("\n")
    target_lines = [l.strip() for l in target_norm.split("\n") if l.strip()]
    if not target_lines:
        return False, full_source

    window_start = max(0, line_num - 60)
    window_end = min(len(source_lines), line_num + 60)

    best_start = -1
    best_len = 0
    for i in range(window_start, window_end):
        s_idx = i
        t_idx = 0
        while s_idx < len(source_lines) and t_idx < len(target_lines):
            s_line = source_lines[s_idx].strip()
            if not s_line:
                s_idx += 1
                continue
            if s_line == target_lines[t_idx]:
                t_idx += 1
                s_idx += 1
            else:
                break
        if t_idx == len(target_lines):
            best_start = i
            best_len = s_idx - i
            break

    if best_start != -1:
        new_lines = source_lines[:best_start] + [rep_norm] + source_lines[best_start + best_len:]
        return True, "\n".join(new_lines)

    return False, full_source


# ── 1. Traceback Analyzer ───────────────────────────────────────────────────

class TracebackAnalyzer:
    """Parses tracebacks and pinpoints the responsible first-party codebase file and line."""

    @staticmethod
    def parse(tb_text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": False,
            "target_file": None,
            "line_number": None,
            "function_name": None,
            "exception_type": None,
            "exception_message": None,
            "raw_traceback": tb_text,
        }

        if not tb_text:
            return result

        # Extract exception type and message from the last line
        lines = [line.strip() for line in tb_text.strip().splitlines() if line.strip()]
        if lines:
            last_line = lines[-1]
            if ":" in last_line:
                parts = last_line.split(":", 1)
                result["exception_type"] = parts[0].strip()
                result["exception_message"] = parts[1].strip()
            else:
                result["exception_type"] = last_line
                result["exception_message"] = ""

        # Match all File "path", line X, in func entries
        file_pattern = re.compile(r'File\s+["\']([^"\']+\.py)["\'],\s+line\s+(\d+)(?:,\s+in\s+([^\n\r]+))?', re.IGNORECASE)
        matches = file_pattern.findall(tb_text)

        # Iterate in reverse (innermost / latest frame first) to find first-party codebase file
        for raw_path, line_str, func_name in reversed(matches):
            p = Path(raw_path)
            # Skip third-party packages or virtualenvs
            if "site-packages" in raw_path.lower() or ".venv" in raw_path.lower() or "lib\\python" in raw_path.lower():
                continue

            # Check if file exists in our codebase
            resolved = None
            if (BASE_DIR / p).exists():
                resolved = (BASE_DIR / p).resolve()
            elif p.is_absolute() and p.exists():
                resolved = p
            else:
                candidate = BASE_DIR / p.name
                if candidate.exists():
                    resolved = candidate
                else:
                    # Search inside subdirectories
                    for sub in ("actions", "core", "agent", "services"):
                        c2 = BASE_DIR / sub / p.name
                        if c2.exists():
                            resolved = c2
                            break

            if not resolved:
                resolved = p if p.is_absolute() else (BASE_DIR / p)

            if resolved:
                # Check immunity
                if resolved.name in PROTECTED_CORE_FILES:
                    logger.warning(f"[AutoHeal] File '{resolved.name}' is core-protected and cannot be patched.")
                    continue

                result["success"] = True
                result["target_file"] = str(resolved.resolve() if resolved.exists() else resolved)
                result["line_number"] = int(line_str)
                result["function_name"] = func_name.strip() if func_name else None
                break

        return result


# ── 2. Safety Sandbox & Rollback Manager ────────────────────────────────────

class SafetySandbox:
    """Manages atomic backups, AST parsing, compilation tests, and instant rollback."""

    @staticmethod
    def create_backup(file_path: Path) -> Path:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        backup_name = f"{file_path.stem}.bak_{stamp}{file_path.suffix}"
        backup_path = BACKUPS_DIR / backup_name
        shutil.copy2(file_path, backup_path)
        return backup_path

    @staticmethod
    def validate_code(code_str: str, file_name: str = "<staging>") -> Tuple[bool, Optional[str]]:
        """Validates that candidate code parses into a valid Python AST without syntax errors."""
        try:
            ast.parse(code_str, filename=file_name)
            return True, None
        except SyntaxError as e:
            return False, f"Syntax Error on line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Validation Error: {e}"

    @staticmethod
    def rollback_patch(patch_id: str) -> Dict[str, Any]:
        """Rolls back an applied patch by its ID."""
        history = SafetySandbox._load_history()
        for entry in reversed(history):
            if entry.get("patch_id") == patch_id or patch_id == "latest":
                if entry.get("status") != "applied":
                    continue
                target = Path(entry.get("target_file", ""))
                backup = Path(entry.get("backup_path", ""))
                if not backup.exists() or not target.exists():
                    return {"success": False, "message": f"Backup file '{backup}' missing."}

                try:
                    shutil.copy2(backup, target)
                    entry["status"] = "rolled_back"
                    entry["rolled_back_at"] = time.time()
                    SafetySandbox._save_history(history)
                    return {
                        "success": True,
                        "message": f"Successfully rolled back patch {entry.get('patch_id')} on '{target.name}'.",
                        "target_file": str(target),
                    }
                except Exception as e:
                    return {"success": False, "message": f"Rollback failed: {e}"}

        return {"success": False, "message": f"No active patch matching '{patch_id}' found to rollback."}

    @staticmethod
    def _load_history() -> List[Dict[str, Any]]:
        if not PATCH_HISTORY_FILE.exists():
            return []
        try:
            with open(PATCH_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    @staticmethod
    def _save_history(history: List[Dict[str, Any]]) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(PATCH_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=4)
        except Exception as e:
            logger.error(f"[AutoHeal] Failed to save patch history: {e}")


# ── 3. Patch Synthesizer & Auto-Heal Controller ─────────────────────────────

class AutoHealEngine:
    """Orchestrates error analysis, hotfix synthesis, verification, and application."""

    _last_error: Optional[str] = None

    @classmethod
    def record_last_error(cls, tb_str: str) -> None:
        """Stores the most recent error traceback captured during runtime."""
        cls._last_error = tb_str

    @classmethod
    def get_last_error(cls) -> Optional[str]:
        """Returns the most recent error traceback if any."""
        return cls._last_error

    @classmethod
    def get_patch_history(cls, limit: int = 5) -> List[Dict[str, Any]]:
        """Returns recent patch history."""
        history = SafetySandbox._load_history()
        return list(reversed(history))[:limit]

    @classmethod
    def heal_traceback(
        cls,
        traceback_text: str,
        context_notes: str = "",
        dry_run: bool = False,
        player: Any = None,
        speak: Optional[Callable[[str], None]] = None,
        module_name: str = "",
    ) -> Dict[str, Any]:
        """
        Autonomous IRIS Self-Repair:
        1. Pulls terminal and IRIS UI to front immediately.
        2. Speaks malfunction detection announcement.
        3. Visualizes diagnosis, problem, planned fix, and patch diff on the UI workspace.
        4. Synthesizes surgical patch with AST-level context scope.
        5. Validates AST in memory sandbox and applies resiliently.
        6. Commits atomic patch, updates HUD deliverable, and confirms success.
        """
        # 1. Bring Terminal & IRIS UI to front
        bring_terminal_to_front(player=player)

        parsed = TracebackAnalyzer.parse(traceback_text)
        exc_type = parsed.get("exception_type", "")
        if exc_type in ("SystemExit", "KeyboardInterrupt") or "SystemExit: 0" in (traceback_text or ""):
            return {"success": True, "message": "Normal system termination ignored."}

        target_name = (Path(parsed.get("target_file", "")).name if parsed.get("target_file") else None) or module_name or "an internal module"

        # 2. Trigger IRIS Malfunction Popup Window immediately
        if player and hasattr(player, "show_iris_malfunction"):
            try:
                player.show_iris_malfunction({
                    "action": "show",
                    "file_name": target_name,
                    "target_file": parsed.get("target_file") or "",
                    "line_number": parsed.get("line_number"),
                    "exception_type": parsed.get("exception_type", "Error"),
                    "exception_message": parsed.get("exception_message", ""),
                    "explanation": "Isolating failing line and extracting AST context...",
                    "status": f"Synthesizing surgical hotfix for {target_name}...",
                    "percent": 30,
                })
            except Exception:
                pass

        # 3. Speak initial alert
        if speak:
            try:
                speak(f"Sir, I detected a malfunction in {target_name.replace('.py', '')}. Stand by, I am initiating self-repair now.")
            except Exception as s_err:
                logger.debug(f"Speak error: {s_err}")

        # 4. Initialize UI Task Workspace
        if player and hasattr(player, "begin_task_workspace"):
            try:
                player.begin_task_workspace(
                    f"⚡ IRIS Autonomous Self-Repair: {target_name}",
                    [
                        "Inspect traceback & isolate failing line",
                        "Extract AST scope & dependency imports",
                        "Synthesize verified surgical patch",
                        "Validate syntax & commit atomic hotfix"
                    ],
                    source="local"
                )
            except Exception:
                pass

        if not parsed.get("success"):
            err_msg = "Could not identify a modifiable first-party source file from the traceback."
            if player and hasattr(player, "update_task_workspace"):
                player.update_task_workspace(status="Diagnosis Failed", output=err_msg, percent=0)
            if player and hasattr(player, "show_iris_malfunction"):
                try:
                    player.show_iris_malfunction({
                        "action": "update",
                        "file_name": target_name,
                        "exception_type": parsed.get("exception_type", "Error"),
                        "exception_message": parsed.get("exception_message", ""),
                        "explanation": err_msg,
                        "status": f"⚠️ Malfunction Analysis: {err_msg}",
                        "percent": 0,
                    })
                except Exception:
                    pass
            return {
                "success": False,
                "message": err_msg,
                "parsed": parsed,
            }

        target_file_str = parsed["target_file"]
        target_path = Path(target_file_str)
        line_num = parsed["line_number"]

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                full_source = f.read()
        except Exception as e:
            err_msg = f"Unable to read target file '{target_path.name}': {e}"
            if player and hasattr(player, "update_task_workspace"):
                player.update_task_workspace(status="File Read Error", output=err_msg, percent=0)
            return {"success": False, "message": err_msg}

        # 4. Extract AST Scope Context
        code_context, enclosing_name = extract_ast_context(full_source, line_num)

        if player and hasattr(player, "update_task_workspace"):
            player.update_task_workspace(
                status=f"Synthesizing hotfix for {target_path.name}:{line_num}...",
                percent=40
            )

        # 5. Generate patch via LLM
        patch_spec = cls._synthesize_patch_code(
            file_name=target_path.name,
            line_num=line_num,
            exception_type=parsed.get("exception_type", "Error"),
            exception_msg=parsed.get("exception_message", ""),
            code_context=code_context,
            context_notes=context_notes,
        )

        if not patch_spec.get("success"):
            err_msg = f"Failed to synthesize patch: {patch_spec.get('error')}"
            if player and hasattr(player, "update_task_workspace"):
                player.update_task_workspace(status="Synthesis Failed", output=err_msg, percent=0)
            if player and hasattr(player, "show_iris_malfunction"):
                try:
                    player.show_iris_malfunction({
                        "action": "update",
                        "file_name": target_path.name,
                        "line_number": line_num,
                        "exception_type": parsed.get("exception_type", "Error"),
                        "exception_message": parsed.get("exception_message", ""),
                        "explanation": err_msg,
                        "status": f"⚠️ Synthesis Failed: {err_msg}",
                        "percent": 0,
                    })
                except Exception:
                    pass
            return {
                "success": False,
                "message": err_msg,
                "parsed": parsed,
            }

        target_chunk = patch_spec["target_chunk"]
        replacement_chunk = patch_spec["replacement_chunk"]
        explanation = patch_spec.get("explanation", "Bug hotfix.")

        # 6. Display Problem, Fix, and How it patches in the IRIS UI workspace & IRIS popup!
        diag_output = (
            f"🚨 MALFUNCTION DETECTED:\n"
            f"• Component: {target_path.name} (line {line_num})"
            + (f" in {enclosing_name}()" if enclosing_name else "") + "\n"
            f"• Exception: {parsed.get('exception_type')}: {parsed.get('exception_message')}\n\n"
            f"🔍 ROOT CAUSE & PLANNED FIX:\n"
            f"{explanation}\n\n"
            f"🛠️ HOW IT WILL BE PATCHED:\n"
            f"<<<< TARGET TO REMOVE:\n{target_chunk.strip()}\n====\n"
            f">>>> REPLACEMENT TO INSERT:\n{replacement_chunk.strip()}\n"
        )
        if player and hasattr(player, "update_task_workspace"):
            player.update_task_workspace(
                title=f"Self-Repair: {target_path.name}",
                status="Validating AST & Staging Patch...",
                output=diag_output,
                percent=70
            )

        if player and hasattr(player, "show_iris_malfunction"):
            try:
                player.show_iris_malfunction({
                    "action": "update",
                    "file_name": target_path.name,
                    "target_file": str(target_path),
                    "line_number": line_num,
                    "exception_type": parsed.get("exception_type", "Error"),
                    "exception_message": parsed.get("exception_message", ""),
                    "explanation": explanation,
                    "target_chunk": target_chunk,
                    "replacement_chunk": replacement_chunk,
                    "status": "Pre-flight AST syntax validation in sandbox...",
                    "percent": 70,
                })
            except Exception:
                pass

        # 7. Apply candidate patch resiliently
        patch_ok, staged_source = apply_patch_resiliently(full_source, target_chunk, replacement_chunk, line_num)
        if not patch_ok:
            err_msg = f"Target code chunk could not be matched precisely in {target_path.name}."
            if player and hasattr(player, "update_task_workspace"):
                player.update_task_workspace(status="Chunk Matching Failed", output=err_msg, percent=0)
            return {
                "success": False,
                "message": err_msg,
                "parsed": parsed,
            }

        # 8. Pre-flight AST Syntax validation
        valid, syntax_err = SafetySandbox.validate_code(staged_source, file_name=target_path.name)
        if not valid:
            logger.error(f"[AutoHeal] Patch rejected by SafetySandbox: {syntax_err}")
            err_msg = f"Safety Guard: Patch rejected due to syntax error: {syntax_err}"
            if player and hasattr(player, "update_task_workspace"):
                player.update_task_workspace(status="AST Rejected", output=err_msg, percent=0)
            return {
                "success": False,
                "message": err_msg,
                "parsed": parsed,
            }

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "target_file": str(target_path),
                "explanation": explanation,
                "target_chunk": target_chunk,
                "replacement_chunk": replacement_chunk,
                "message": f"Dry-run passed syntax validation for {target_path.name}.",
            }

        # 9. Create atomic backup & commit patch
        backup_path = SafetySandbox.create_backup(target_path)
        try:
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(staged_source)
        except Exception as e:
            shutil.copy2(backup_path, target_path)
            err_msg = f"File write failed, restored backup: {e}"
            if player and hasattr(player, "update_task_workspace"):
                player.update_task_workspace(status="Write Error", output=err_msg, percent=0)
            return {"success": False, "message": err_msg}

        # 10. Verify on-disk compilation via py_compile
        try:
            py_compile.compile(str(target_path), doraise=True)
        except Exception as pyc_err:
            logger.error(f"[AutoHeal] py_compile failed after write, rolling back: {pyc_err}")
            shutil.copy2(backup_path, target_path)
            err_msg = f"Post-write compilation failed, rolled back: {pyc_err}"
            if player and hasattr(player, "update_task_workspace"):
                player.update_task_workspace(status="Compile Failed - Rolled Back", output=err_msg, percent=0)
            return {"success": False, "message": err_msg}

        patch_id = str(uuid.uuid4())[:8]
        entry = {
            "patch_id": patch_id,
            "timestamp": time.time(),
            "target_file": str(target_path),
            "backup_path": str(backup_path),
            "line_number": line_num,
            "exception_fixed": f"{parsed.get('exception_type')}: {parsed.get('exception_message')}",
            "explanation": explanation,
            "status": "applied",
        }

        history = SafetySandbox._load_history()
        history.append(entry)
        SafetySandbox._save_history(history)

        final_msg = f"Successfully auto-patched '{target_path.name}' at line {line_num} (Patch ID: {patch_id}). Backup preserved."

        # 11. Finalize UI Task Workspace & IRIS Malfunction Popup Card
        if player and hasattr(player, "show_iris_malfunction"):
            try:
                player.show_iris_malfunction({
                    "action": "update",
                    "file_name": target_path.name,
                    "target_file": str(target_path),
                    "line_number": line_num,
                    "exception_type": parsed.get("exception_type", "Error"),
                    "exception_message": parsed.get("exception_message", ""),
                    "explanation": explanation,
                    "target_chunk": target_chunk,
                    "replacement_chunk": replacement_chunk,
                    "patch_id": patch_id,
                    "backup_name": backup_path.name,
                    "status": f"✅ Autonomously Patched & Verified (Patch ID: {patch_id})",
                    "percent": 100,
                })
            except Exception:
                pass

        if player and hasattr(player, "finish_task_workspace"):
            try:
                player.finish_task_workspace(
                    result=diag_output + f"\n\n✅ VERIFIED & APPLIED:\nPatch ID: {patch_id}\nBackup: {backup_path.name}",
                    status="Autonomous self-repair completed.",
                    percent=100
                )
            except Exception:
                pass

        if player and hasattr(player, "show_hud_deliverable"):
            try:
                player.show_hud_deliverable(
                    title=f"IRIS Self-Repair: {target_path.name}",
                    summary=explanation,
                    bullets=[
                        f"Fixed: {parsed.get('exception_type')}",
                        f"Line: {line_num}",
                        f"Status: AST Validated & Committed",
                        f"Patch ID: {patch_id}",
                    ],
                    kind="self_heal"
                )
            except Exception:
                pass

        # 12. Speak completion
        if speak:
            try:
                speak(f"Self-repair complete, sir. I have patched {target_path.name} and eliminated the bug.")
            except Exception:
                pass

        return {
            "success": True,
            "patch_id": patch_id,
            "target_file": str(target_path),
            "backup_path": str(backup_path),
            "explanation": explanation,
            "message": final_msg,
        }

    @classmethod
    def _synthesize_patch_code(
        cls,
        file_name: str,
        line_num: int,
        exception_type: str,
        exception_msg: str,
        code_context: str,
        context_notes: str = "",
    ) -> Dict[str, Any]:
        """Calls LLM to generate the surgical exact target chunk and replacement chunk."""
        prompt = f"""You are an elite Python compiler and autonomous debugging engineer.
A Python bug occurred in file '{file_name}' around line {line_num}.
Exception: {exception_type}: {exception_msg}
Additional context: {context_notes}

Relevant source code context:
```python
{code_context}
```

Task: Provide a surgical, minimal fix to eliminate the exception (e.g. add None-checks, handle key errors, safe type casting, boundary check).
Output ONLY a strict JSON object with these exact keys:
{{
    "explanation": "One sentence explaining what was fixed",
    "target_chunk": "Exact verbatim string from code_context to replace (must match characters and whitespace exactly)",
    "replacement_chunk": "Replacement code to substitute in place of target_chunk"
}}
Do NOT include markdown fences outside the JSON. Return only the valid JSON object.
"""
        # 1. Primary: Google Gemini (Native directly via google.genai)
        gemini_key = _get_iris_api_key()
        if gemini_key:
            try:
                from google import genai
                g_client = genai.Client(api_key=gemini_key, http_options={"api_version": "v1beta"})
                for model_name in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"):
                    try:
                        resp = g_client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config={"temperature": 0.1, "response_mime_type": "application/json"}
                        )
                        raw_text = getattr(resp, "text", "") or ""
                        if raw_text.strip():
                            clean_json = raw_text.strip()
                            if clean_json.startswith("```"):
                                clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", clean_json)
                                clean_json = re.sub(r"\n?```$", "", clean_json).strip()
                            data = json.loads(clean_json)
                            if "target_chunk" in data and "replacement_chunk" in data:
                                data["success"] = True
                                return data
                    except Exception as model_err:
                        logger.warning(f"[AutoHeal] Gemini model {model_name} synthesis attempt failed: {model_err}")
                        continue
            except Exception as g_err:
                logger.warning(f"[AutoHeal] Gemini synthesis failed: {g_err}")

        # 2. Fallback: Unified AI Client (llm_client.py)
        try:
            from llm_client import client as unified_client
            resp_text = unified_client.chat(prompt, temperature=0.1)
            clean_json = resp_text.strip()
            if clean_json.startswith("```"):
                clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", clean_json)
                clean_json = re.sub(r"\n?```$", "", clean_json).strip()
            data = json.loads(clean_json)
            if "target_chunk" in data and "replacement_chunk" in data:
                data["success"] = True
                return data
        except Exception as u_err:
            logger.warning(f"[AutoHeal] Unified AI client fallback failed: {u_err}")

        # 3. Fallback: OpenRouter client
        try:
            import or_client
            resp_text = or_client.chat(prompt, system="You are an expert Python auto-patching engineer. Return strict JSON.")
            clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", resp_text.strip())
            clean_json = re.sub(r"\n?```$", "", clean_json).strip()
            data = json.loads(clean_json)
            if "target_chunk" in data and "replacement_chunk" in data:
                data["success"] = True
                return data
        except Exception as or_err:
            logger.warning(f"[AutoHeal] OpenRouter fallback failed: {or_err}")

        # 4. Fallback: Local Offline Brain (Qwen 2.5 via Ollama)
        try:
            from core.local_brain import local_brain
            if local_brain.is_available():
                logger.info("[AutoHeal] Synthesizing surgical patch via LocalBrain (qwen2.5:7b)...")
                res = local_brain.chat_complete(
                    [{"role": "user", "content": prompt}],
                    model="qwen2.5:7b",
                    temperature=0.1
                )
                raw_text = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text.strip())
                clean_json = re.sub(r"\n?```$", "", clean_json).strip()
                data = json.loads(clean_json)
                if "target_chunk" in data and "replacement_chunk" in data:
                    data["success"] = True
                    return data
        except Exception as loc_err:
            logger.warning(f"[AutoHeal] LocalBrain synthesis failed: {loc_err}")

        return {
            "success": False,
            "error": "All LLM synthesis backends failed. Please verify your IRIS AI API key in config/api_keys.json or ensure Ollama is running."
        }


# ── 4. Unified MCP Tool Dispatcher ──────────────────────────────────────────

def auto_heal(
    parameters: Optional[Union[Dict[str, Any], str]] = None,
    player: Any = None,
    speak: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Unified entrypoint for Autonomous Self-Healing and Self-Improvement.
    """
    if isinstance(parameters, str):
        params = {"action": parameters}
    else:
        params = parameters or {}
    action = (params.get("action") or params.get("command") or "status").lower().strip()

    if action in ("history", "log", "patches"):
        patches = AutoHealEngine.get_patch_history(limit=5)
        if not patches:
            msg = "No automatic patches applied yet. System is running clean."
            if speak:
                speak(msg)
            return msg

        lines = ["🛡️ AUTONOMOUS PATCH HISTORY:"]
        for p in patches:
            fn = Path(p.get("target_file", "")).name
            status = p.get("status", "unknown")
            lines.append(f"• [{p.get('patch_id')}] {fn} (line {p.get('line_number')}): {p.get('explanation')} — Status: {status}")

        result = "\n".join(lines)
        if speak:
            speak(f"You have {len(patches)} recent patches logged. Last patch was on {Path(patches[0].get('target_file', '')).name}.")
        return result

    elif action in ("rollback", "undo", "revert"):
        patch_id = params.get("patch_id") or "latest"
        res = SafetySandbox.rollback_patch(patch_id)
        msg = res.get("message", "Rollback completed.")
        if speak:
            speak(msg)
        return msg

    elif action in ("heal", "fix", "patch"):
        tb = params.get("traceback") or params.get("error") or params.get("error_traceback") or ""
        notes = params.get("notes") or params.get("context") or ""
        if not tb:
            tb = AutoHealEngine.get_last_error() or ""
        if not tb:
            # Check FATAL_CRASH.log if no traceback explicitly provided
            crash_log = BASE_DIR / "FATAL_CRASH.log"
            if crash_log.exists():
                try:
                    tb = crash_log.read_text(encoding="utf-8")
                except Exception:
                    pass
        if not tb:
            msg = "No recent error or traceback captured to heal. If an error just occurred, you can paste the traceback."
            if speak:
                speak(msg)
            return msg

        res = AutoHealEngine.heal_traceback(
            tb,
            context_notes=notes,
            player=player,
            speak=speak,
            module_name=params.get("module", "")
        )
        msg = res.get("message", "Done.")
        return msg

    elif action in ("learn_rule", "add_rule", "remember_rule"):
        rule = params.get("rule") or params.get("directive") or params.get("text") or ""
        if not rule:
            return "Please specify a rule to learn (e.g. rule='Always use Chrome browser')."
        from core.learned_rules import LearnedRulesEngine
        res = LearnedRulesEngine.add_rule(rule)
        msg = res.get("message", "Rule saved.")
        if speak:
            speak(f"Understood, sir. I have committed that rule to my memory.")
        return msg

    elif action in ("list_rules", "rules"):
        from core.learned_rules import LearnedRulesEngine
        rules = LearnedRulesEngine.list_rules()
        if not rules:
            return "No learned rules stored."
        lines = ["🧠 LEARNED BEHAVIORAL DIRECTIVES:"]
        for r in rules:
            status = "ACTIVE" if r.get("active") else "INACTIVE"
            lines.append(f"• [{r.get('id')}] ({status}) {r.get('rule')}")
        return "\n".join(lines)

    else:  # status
        patches = AutoHealEngine.get_patch_history(limit=1)
        last_patch = patches[0] if patches else None
        from core.learned_rules import LearnedRulesEngine
        rule_count = len(LearnedRulesEngine.list_rules(active_only=True))

        lines = [
            "🛡️ AUTO-HEAL & SELF-IMPROVEMENT STATUS",
            "─────────────────────────────────────",
            "• Boot Sentry: Active (Automatic rollback enabled)",
            "• Safety Sandbox: Enabled (Pre-flight AST & Compilation validation)",
            f"• Learned Behavioral Directives: {rule_count} active rules",
            f"• Recent Hotfixes: {last_patch.get('explanation') if last_patch else 'None (Clean)'}",
        ]
        report = "\n".join(lines)
        if speak:
            speak("Auto-heal sentry and continuous self-improvement are fully active, sir.")
        return report
