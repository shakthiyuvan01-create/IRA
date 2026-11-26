"""Applies approved self-modifications, keeps a reviewable log, and undoes them.

Layout (all under Application/Data/self_improve/, which git ignores):
    log.jsonl              one JSON line per event: applied, undone, failed
    <id>/manifest.json     request, summary, files, model, time
    <id>/change.diff       the exact diff the person approved
    <id>/before/<path>     the original of every file the change touched

Undo restores the originals and deletes files the change created.
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

from .agent import APP_ROOT, Proposal

JOURNAL_DIR = APP_ROOT / "Data" / "self_improve"
LOG_PATH = JOURNAL_DIR / "log.jsonl"
_lock = threading.Lock()


def _log(event: str, **data) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **data}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def apply(proposal: Proposal) -> Path:
    """Write the proposal to disk after backing up every file it touches.
    If any write fails, the files already written are rolled back."""
    with _lock:
        folder = JOURNAL_DIR / proposal.id
        before = folder / "before"
        before.mkdir(parents=True, exist_ok=True)
        (folder / "change.diff").write_text(proposal.diff, encoding="utf-8")
        created, modified = [], []
        for c in proposal.changes:
            target = APP_ROOT / c.path
            if target.exists():
                dest = before / c.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
                modified.append(c.path)
            else:
                created.append(c.path)
        manifest = {
            "id": proposal.id, "request": proposal.request, "summary": proposal.summary,
            "model": proposal.model, "created": created, "modified": modified,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "applied",
        }
        written: list[str] = []
        try:
            for c in proposal.changes:
                target = APP_ROOT / c.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(c.new, encoding="utf-8", newline="\n")
                written.append(c.path)
        except Exception as exc:
            _restore(folder, written, created)
            manifest["status"] = "failed"
            _write_manifest(folder, manifest)
            _log("failed", id=proposal.id, request=proposal.request, error=str(exc))
            raise
        _write_manifest(folder, manifest)
        added, removed = proposal.stats
        _log("applied", id=proposal.id, request=proposal.request, summary=proposal.summary,
             files=created + modified, added=added, removed=removed, model=proposal.model)
        return folder


def _write_manifest(folder: Path, manifest: dict) -> None:
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                          encoding="utf-8")


def _restore(folder: Path, paths: list[str], created: list[str]) -> None:
    for rel in paths:
        target = APP_ROOT / rel
        if rel in created:
            target.unlink(missing_ok=True)
        else:
            shutil.copy2(folder / "before" / rel, target)


def history(limit: int = 10) -> list[dict]:
    """Newest first: the manifests of applied (and undone) changes."""
    if not JOURNAL_DIR.is_dir():
        return []
    out = []
    for m in JOURNAL_DIR.glob("*/manifest.json"):
        try:
            out.append(json.loads(m.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda d: d.get("id", ""), reverse=True)
    return out[:limit]


def undo(change_id: str | None = None) -> dict:
    """Revert one change (default: the newest still-applied one)."""
    with _lock:
        items = [h for h in history(200) if h.get("status") == "applied"]
        if change_id:
            items = [h for h in items if h["id"] == change_id]
        if not items:
            raise LookupError("There is no applied self-improvement to undo.")
        manifest = items[0]
        folder = JOURNAL_DIR / manifest["id"]
        _restore(folder, manifest["created"] + manifest["modified"], manifest["created"])
        manifest["status"] = "undone"
        manifest["undone_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_manifest(folder, manifest)
        _log("undone", id=manifest["id"], request=manifest["request"],
             files=manifest["created"] + manifest["modified"])
        return manifest


def hot_load(paths: list[str]) -> tuple[list[str], dict[str, str]]:
    """Load new or changed skills into the running IRA. Returns (loaded tool
    names, {skill: error}). Works only for files in actions/ingested/."""
    from actions import ingested
    for rel in paths:
        stem = Path(rel).stem
        sys.modules.pop(f"actions.ingested.{stem}", None)
    tools = ingested.registry(refresh=True)
    errors = {k: v for k, v in ingested.errors.items()
              if any(Path(p).stem == k for p in paths)}
    loaded = []
    main = sys.modules.get("__main__")
    register = getattr(main, "_load_ingested_tools", None)
    if callable(register):                 # adds declarations the model can see
        register()
    stems = {Path(p).stem for p in paths}
    for name, tool in tools.items():
        module = getattr(tool.get("handler"), "__module__", "")
        if module.rsplit(".", 1)[-1] in stems:
            loaded.append(name)
    return loaded, errors
