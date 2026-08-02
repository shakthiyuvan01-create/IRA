"""
folder_analyzer.py — Point IRA at a path and it explains what's there.

- Given a FOLDER path: lists contents with each item's type, size, and a short
  human description, plus a summary (how many files/folders, by category).
- Given a FILE path: tells Yuvan what that file is (type + size + description).
"""
import os
from datetime import datetime
from pathlib import Path

# Extension → human description.
_EXT_INFO = {
    ".py": "Python script", ".js": "JavaScript file", ".ts": "TypeScript file",
    ".html": "web page", ".css": "stylesheet", ".json": "JSON data file",
    ".txt": "text file", ".md": "Markdown document", ".csv": "CSV spreadsheet",
    ".xlsx": "Excel spreadsheet", ".xls": "Excel spreadsheet", ".xlsm": "Excel macro workbook",
    ".doc": "Word document", ".docx": "Word document", ".pdf": "PDF document",
    ".ppt": "PowerPoint presentation", ".pptx": "PowerPoint presentation",
    ".png": "PNG image", ".jpg": "JPEG image", ".jpeg": "JPEG image",
    ".gif": "GIF image", ".bmp": "bitmap image", ".svg": "vector image",
    ".ico": "icon file", ".webp": "WebP image",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".mkv": "video",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".m4a": "audio",
    ".zip": "compressed archive", ".rar": "compressed archive", ".7z": "compressed archive",
    ".exe": "Windows program", ".msi": "Windows installer", ".bat": "batch script",
    ".ps1": "PowerShell script", ".sh": "shell script", ".vbs": "VBScript",
    ".sql": "SQL database script", ".db": "database file", ".sqlite": "SQLite database",
    ".c": "C source", ".cpp": "C++ source", ".java": "Java source", ".rs": "Rust source",
    ".go": "Go source", ".rb": "Ruby script", ".php": "PHP script",
    ".log": "log file", ".ini": "configuration file", ".cfg": "configuration file",
    ".yml": "YAML config", ".yaml": "YAML config", ".xml": "XML file",
    ".ttf": "font", ".otf": "font",
}


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _describe_file(p: Path) -> str:
    ext = p.suffix.lower()
    return _EXT_INFO.get(ext, (ext[1:].upper() + " file") if ext else "file")


def _resolve(path: str) -> Path:
    shortcuts = {
        "desktop":   Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "home":      Path.home(),
    }
    key = (path or "").strip().lower()
    if key in shortcuts:
        return shortcuts[key]
    return Path(os.path.expanduser(os.path.expandvars(path.strip().strip('"'))))


def analyze_folder(parameters=None, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    raw    = (params.get("path") or params.get("folder") or "").strip()
    if not raw:
        return "Please give me a folder or file path to analyse, Yuvan."

    target = _resolve(raw)

    if not target.exists():
        return f"I couldn't find that path, Yuvan: {target}"

    # ── Single file ─────────────────────────────────────────────────────────
    if target.is_file():
        try:
            size = _human_size(target.stat().st_size)
            mod  = datetime.fromtimestamp(target.stat().st_mtime).strftime("%d %b %Y")
        except Exception:
            size, mod = "?", "?"
        return (
            f"That's a {_describe_file(target)}, Yuvan.\n"
            f"Name: {target.name}\nSize: {size}\nLast modified: {mod}\n"
            f"Location: {target.parent}"
        )

    # ── Folder ──────────────────────────────────────────────────────────────
    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return f"I don't have permission to open that folder, Yuvan: {target}"
    except Exception as e:
        return f"I couldn't read that folder, Yuvan: {e}"

    if not entries:
        return f"The folder '{target.name}' is empty, Yuvan."

    folders, files = [], []
    total_size = 0
    cat_counts: dict[str, int] = {}

    for e in entries:
        try:
            if e.is_dir():
                folders.append(f"📁 {e.name}  (folder)")
            else:
                sz = e.stat().st_size
                total_size += sz
                desc = _describe_file(e)
                cat_counts[desc] = cat_counts.get(desc, 0) + 1
                files.append(f"📄 {e.name}  —  {desc}, {_human_size(sz)}")
        except Exception:
            continue

    lines = [f"Here's what's in '{target.name}', Yuvan:", ""]
    lines.append(f"{len(folders)} folder(s), {len(files)} file(s), "
                 f"{_human_size(total_size)} total.")
    if cat_counts:
        top = sorted(cat_counts.items(), key=lambda t: -t[1])[:6]
        lines.append("Mostly: " + ", ".join(f"{c}× {d}" for d, c in top) + ".")
    lines.append("")

    shown = (folders + files)[:40]
    lines.extend(shown)
    if len(folders) + len(files) > 40:
        lines.append(f"…and {len(folders) + len(files) - 40} more.")

    return "\n".join(lines)
