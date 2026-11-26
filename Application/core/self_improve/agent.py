"""Plans a change to IRA's own code from a plain-language request.

The agent never writes to disk. It returns a Proposal (the files it would
create or edit, and a unified diff); core/self_improve/journal.py applies it
once the person approves it in the Self Improve panel.

Two kinds of request:
  * a new skill ("add a writing skill")  -> a new tool in actions/ingested/,
    which IRA can load without a restart
  * an improvement ("improve the weather report") -> edits to existing files,
    which take effect after a restart

Every proposal is checked before it is shown: paths stay inside the app and
away from secrets, every edit must match the file exactly once, Python must
parse, and a new skill must import and expose a valid TOOL dict.
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

APP_ROOT = Path(__file__).resolve().parents[2]          # .../Application
SKILLS_DIR = APP_ROOT / "actions" / "ingested"

_ALLOWED_EXT = {".py", ".md", ".txt", ".json", ".html", ".css", ".js"}
# Never touched by a self-modification: secrets, settings, the runtime, the
# approval gate itself, and anything generated.
_BLOCKED = (
    ".env", "core/config/", "core/self_improve/", "actions/ingested/self_improve.py", "Data/", "memory/",
    "Runtime/", "__pycache__/", ".git/", "core/auth/",
)
_INDEX_DIRS = ("actions", "actions/ingested", "core", "core/tasks", "core/providers",
               "core/routines", "core/memory", "agents")
_MAX_FILE_CHARS = 40_000           # larger files are sent as an outline + excerpts
Progress = Callable[[str], None]


# ─────────────────────────────────────────────────────────────────────────────
#  Data
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Change:
    path: str                     # relative to APP_ROOT, forward slashes
    old: str | None               # None for a new file
    new: str

    @property
    def is_new(self) -> bool:
        return self.old is None

    def diff(self) -> str:
        a = [] if self.old is None else self.old.splitlines(keepends=True)
        b = self.new.splitlines(keepends=True)
        return "".join(difflib.unified_diff(
            a, b,
            fromfile="/dev/null" if self.old is None else f"a/{self.path}",
            tofile=f"b/{self.path}", n=3,
        ))


@dataclass
class Proposal:
    request: str
    summary: str
    changes: list[Change]
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    model: str = ""

    @property
    def diff(self) -> str:
        return "\n".join(c.diff() for c in self.changes)

    @property
    def hot_loadable(self) -> bool:
        """New or changed skills only: IRA can load them without a restart."""
        return all(c.path.startswith("actions/ingested/") and c.path.endswith(".py")
                   for c in self.changes)

    @property
    def stats(self) -> tuple[int, int]:
        added = removed = 0
        for line in self.diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return added, removed


class PlanError(RuntimeError):
    """The request could not be turned into a safe, valid change."""


# ─────────────────────────────────────────────────────────────────────────────
#  Model access
# ─────────────────────────────────────────────────────────────────────────────

def _ask_model(prompt: str, system: str, max_tokens: int = 8000) -> tuple[str, str]:
    """Return (text, provider). Code generation is slow, so the coding call
    gets a long timeout of its own rather than the 30 s chat budget."""
    key = os.getenv("XKIRO_API_KEY", "").strip()
    if key:
        try:
            from core.providers import xkiro
            model = os.getenv("XKIRO_MODEL", "").strip() or xkiro.DEFAULT_MODEL
            data = xkiro._post({
                "model": model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "temperature": 0.2,
            }, key, timeout=240)
            text = (data["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text, f"xkiro/{model}"
        except Exception as exc:                       # fall through to the chain
            print(f"[SelfImprove] xkiro failed, using provider chain: {exc}")
    from core.providers.manager import ProviderManager
    pm = ProviderManager()
    text = pm.generate(prompt, system=system, max_tokens=max_tokens, temperature=0.2)
    if not text or text.startswith("[AI error"):
        raise PlanError(f"No AI provider answered: {text or 'empty reply'}")
    return text, pm.last_used or "provider chain"


# ─────────────────────────────────────────────────────────────────────────────
#  Codebase context
# ─────────────────────────────────────────────────────────────────────────────

def _rel(path: Path) -> str:
    return path.relative_to(APP_ROOT).as_posix()


def _first_doc_line(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:3000]
    except OSError:
        return ""
    m = re.search(r'^\s*(?:#.*\n)*\s*[ru]?("""|\'\'\')\s*(.+?)\s*$', text, re.MULTILINE)
    if m:
        return m.group(2).strip()[:110]
    m = re.search(r'"description"\s*:\s*\(?\s*["\'](.+?)["\']', text)
    return m.group(1)[:110] if m else ""


def codebase_index() -> str:
    lines = []
    for d in _INDEX_DIRS:
        folder = APP_ROOT / d
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.py")):
            if p.name.startswith("_") and p.name != "__init__.py":
                continue
            rel = _rel(p)
            if is_blocked(rel):
                continue
            lines.append(f"{rel} — {_first_doc_line(p)}")
    return "\n".join(lines)


def _keywords(text: str) -> list[str]:
    stop = {"improve", "add", "the", "a", "an", "to", "skill", "feature", "make", "it",
            "and", "for", "of", "with", "ira", "please", "new", "better", "so", "that",
            "can", "my", "me", "in", "on", "is", "be", "should", "when", "create"}
    return [w for w in re.findall(r"[a-z_]{3,}", text.lower()) if w not in stop][:8]


def file_context(rel: str, request: str) -> str:
    path = APP_ROOT / rel
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= _MAX_FILE_CHARS:
        return text
    # Large file: outline of definitions plus windows around request keywords.
    lines = text.splitlines()
    outline = [f"{i+1}: {l.strip()}" for i, l in enumerate(lines)
               if re.match(r"\s*(class|def|async def)\s", l)]
    keys = _keywords(request)
    hits: list[int] = []
    for i, l in enumerate(lines):
        if any(k in l.lower() for k in keys):
            hits.append(i)
    windows, used = [], set()
    for h in hits[:12]:
        lo, hi = max(0, h - 25), min(len(lines), h + 25)
        if any(j in used for j in range(lo, hi)):
            continue
        used.update(range(lo, hi))
        windows.append("\n".join(lines[lo:hi]))
    return ("[Large file: outline and the parts that match the request. Only quote "
            "FIND text that appears verbatim below.]\nOUTLINE:\n" + "\n".join(outline[:250])
            + "\n\nEXCERPTS:\n" + "\n...\n".join(windows))


def is_blocked(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("/")
    return any(rel == b.rstrip("/") or rel.startswith(b) or f"/{b}" in f"/{rel}"
               for b in _BLOCKED)


def check_path(rel: str) -> str:
    rel = rel.strip().strip("`'\"").replace("\\", "/").removeprefix("./")
    if rel.startswith("Application/"):
        rel = rel[len("Application/"):]
    target = (APP_ROOT / rel).resolve()
    if APP_ROOT.resolve() not in target.parents:
        raise PlanError(f"{rel} is outside IRA's folder.")
    if is_blocked(rel):
        raise PlanError(f"{rel} is protected (settings, secrets or the self-improve gate).")
    if target.suffix.lower() not in _ALLOWED_EXT:
        raise PlanError(f"{rel}: only {', '.join(sorted(_ALLOWED_EXT))} files can be changed.")
    return rel


# ─────────────────────────────────────────────────────────────────────────────
#  Prompts
# ─────────────────────────────────────────────────────────────────────────────

_SKILL_TEMPLATE = '''"""<One line: what this skill does.>"""
from __future__ import annotations


def handler(parameters=None, player=None):
    parameters = parameters or {}
    topic = str(parameters.get("topic", "")).strip()
    # ... do the work with the standard library or packages IRA already uses ...
    result = f"..."
    if player is not None and hasattr(player, "show_content"):
        player.show_content("TITLE", result)      # optional: show long output on screen
    return result                                  # what IRA says back


TOOL = {
    "name": "snake_case_name",
    "description": "When IRA should use this tool, in one or two sentences.",
    "parameters": {
        "type": "OBJECT",
        "properties": {"topic": {"type": "STRING", "description": "..."}},
        "required": ["topic"],
    },
    "handler": handler,
}
'''

_SYSTEM = f"""You are IRA's self-improvement engineer. IRA is a Windows desktop voice
assistant written in Python 3.12 with PyQt6. You change IRA's own source code to do
what the person asks. Write complete, working, production-quality code. No
placeholders, no TODOs, no emojis.

HOW IRA IS BUILT
- The app root contains actions/ (tools), core/ (engine, UI in core/ui.py, entry
  core/main.py), agents/.
- A NEW SKILL is one new file in actions/ingested/<snake_name>.py with a
  module-level TOOL dict. IRA loads it as a tool the model can call, without a
  restart. Template:
{_SKILL_TEMPLATE}
- Text generation inside a skill: `from core.providers.manager import ProviderManager`
  then `ProviderManager().generate(prompt, system="...", max_tokens=1500)` returns a str.
- Opening apps/files on Windows: os.startfile or subprocess. Prefer the standard
  library and packages already imported elsewhere in IRA. Never install packages.
- Never touch .env, core/config/, core/self_improve/, Data/, memory/.
- When the person asks to ADD a skill, feature or ability, CREATE one new file in
  actions/ingested/ and do not edit existing files: import and reuse existing
  functions instead. Edit existing files only when asked to improve, change or fix
  something that already exists.

OUTPUT FORMAT (exactly this, nothing else):
SUMMARY: <one or two plain sentences for the person: what you changed and how to use it>
=== CREATE <relative/path.py>
<full file content>
=== END
=== EDIT <relative/path.py>
<<<<<<< FIND
<exact text copied from the file, enough lines to be unique>
=======
<replacement text>
>>>>>>> REPLACE
=== END

Use CREATE for new files and EDIT for existing ones. An EDIT block may hold several
FIND/REPLACE pairs. Keep edits small and focused. Paths are relative to the app root
(e.g. actions/ingested/writing_assistant.py)."""


def _pick_files(request: str, index: str) -> list[str]:
    prompt = (f"Request: {request}\n\nIRA source files:\n{index}\n\n"
              "Which existing files (at most 4) must be read to make this change? "
              "For a brand-new skill, list only files whose code the new skill should "
              "reuse, or none. Reply with ONLY a JSON array of paths, e.g. "
              '["actions/weather_report.py"].')
    text, _ = _ask_model(prompt, "You pick source files. Reply with a JSON array only.",
                         max_tokens=300)
    m = re.search(r"\[.*?\]", text, re.DOTALL)
    try:
        picked = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        picked = re.findall(r"[\w/]+\.py", text)
    out = []
    for p in picked:
        try:
            rel = check_path(str(p))
        except PlanError:
            continue
        if (APP_ROOT / rel).is_file() and rel not in out:
            out.append(rel)
    return out[:4]


# ─────────────────────────────────────────────────────────────────────────────
#  Parsing and validation
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK_RE = re.compile(r"^=+ *(CREATE|EDIT):? +(.+?)[ \t]*=*[ \t]*\n(.*?)^=+ *END\b[ =]*$",
                       re.MULTILINE | re.DOTALL)
_PAIR_RE = re.compile(r"<<<<<<< FIND\n(.*?)\n=======\n(.*?)\n?>>>>>>> REPLACE",
                      re.DOTALL)


def _strip_fence(body: str) -> str:
    body = body.strip("\n")
    m = re.match(r"^```[\w-]*\n(.*?)\n```$", body, re.DOTALL)
    return (m.group(1) if m else body) + "\n"


def _apply_edit(text: str, find: str, replace: str, rel: str) -> str:
    count = text.count(find)
    if count == 1:
        return text.replace(find, replace, 1)
    if count > 1:
        raise PlanError(f"{rel}: the text to replace appears {count} times.")
    # Tolerate trailing-whitespace drift in the model's copy.
    norm = lambda s: "\n".join(l.rstrip() for l in s.splitlines())
    ntext, nfind = norm(text), norm(find)
    if nfind and ntext.count(nfind) == 1:
        return ntext.replace(nfind, norm(replace), 1) + ("\n" if text.endswith("\n") else "")
    raise PlanError(f"{rel}: could not find the text to replace:\n{find[:200]}")


def parse_reply(reply: str) -> tuple[str, list[Change]]:
    m = re.search(r"SUMMARY:\s*(.+?)(?:\n===|\Z)", reply, re.DOTALL)
    summary = m.group(1).strip() if m else "Proposed change."
    changes: dict[str, Change] = {}
    blocks = _BLOCK_RE.findall(reply)
    if not blocks:
        raise PlanError("The model did not return any file changes.")
    for kind, raw_path, body in blocks:
        rel = check_path(raw_path)
        path = APP_ROOT / rel
        if kind == "CREATE":
            old = path.read_text(encoding="utf-8") if path.is_file() else None
            changes[rel] = Change(rel, old, _strip_fence(body))
            continue
        if rel in changes:
            current, old = changes[rel].new, changes[rel].old
        elif path.is_file():
            old = current = path.read_text(encoding="utf-8")
        else:
            raise PlanError(f"{rel} does not exist, so it cannot be edited.")
        pairs = _PAIR_RE.findall(body)
        if not pairs:
            raise PlanError(f"{rel}: EDIT block has no FIND/REPLACE pair.")
        for find, replace in pairs:
            current = _apply_edit(current, find, replace, rel)
        changes[rel] = Change(rel, old, current)
    real = [c for c in changes.values() if c.old != c.new]
    if not real:
        raise PlanError("The proposed change would not alter any file.")
    return summary, real


def _check_skill(change: Change) -> str | None:
    """Static check of a skill's TOOL dict. Generated code is never run before
    the person approves it, so this reads the syntax tree instead of importing."""
    tree = ast.parse(change.new)
    funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    tool = None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TOOL"
                                                 for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            tool = node.value
    if tool is None:
        return "no module-level TOOL = {...} dict"
    entries = {k.value: v for k, v in zip(tool.keys, tool.values)
               if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    for key in ("name", "description", "parameters", "handler"):
        if key not in entries:
            return f"TOOL is missing '{key}'"
    name = entries["name"]
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)
            and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$", name.value)):
        return "TOOL name must be a snake_case string literal"
    handler = entries["handler"]
    if isinstance(handler, ast.Name) and handler.id not in funcs:
        return f"handler '{handler.id}' is not defined in the file"
    return None


def validate(changes: list[Change]) -> list[str]:
    problems = []
    for c in changes:
        if c.path.endswith(".py"):
            try:
                ast.parse(c.new, filename=c.path)
            except SyntaxError as e:
                problems.append(f"{c.path}: syntax error line {e.lineno}: {e.msg}")
                continue
            if c.path.startswith("actions/ingested/") and not Path(c.path).name.startswith("_"):
                err = _check_skill(c)
                if err:
                    problems.append(f"{c.path}: {err}")
        elif c.path.endswith(".json"):
            try:
                json.loads(c.new)
            except json.JSONDecodeError as e:
                problems.append(f"{c.path}: invalid JSON: {e}")
    return problems


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def plan(request: str, progress: Progress = print) -> Proposal:
    """Turn a request into a validated Proposal. Raises PlanError."""
    request = request.strip()
    if not request:
        raise PlanError("Tell me what to improve or which skill to add.")

    progress("Reading IRA's source layout…")
    index = codebase_index()
    files = _pick_files(request, index)
    if files:
        progress("Reading " + ", ".join(files) + "…")

    context = "\n\n".join(f"----- FILE {rel} -----\n{file_context(rel, request)}"
                          for rel in files)
    existing_skills = ", ".join(p.stem for p in sorted(SKILLS_DIR.glob("*.py"))
                                if not p.stem.startswith("_")) or "none"
    prompt = (f"REQUEST: {request}\n\nExisting skills in actions/ingested: {existing_skills}\n\n"
              f"SOURCE FILE INDEX:\n{index}\n\n{context}")

    progress("Writing the change…")
    reply, model = _ask_model(prompt, _SYSTEM)
    last_error = ""
    for attempt in range(2):
        try:
            summary, changes = parse_reply(reply)
            problems = validate(changes)
            if not problems:
                return Proposal(request=request, summary=summary, changes=changes, model=model)
            last_error = "\n".join(problems)
        except PlanError as e:
            last_error = str(e)
        if attempt == 0:
            progress("First draft had a problem; fixing it…")
            reply, model = _ask_model(
                prompt + "\n\nYOUR PREVIOUS ANSWER:\n" + reply[:30_000]
                + f"\n\nIT FAILED VALIDATION:\n{last_error}\n\n"
                "Return the corrected answer in the same format, complete.", _SYSTEM)
    raise PlanError(last_error)
