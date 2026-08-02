"""Route forge codegen and revision prompts by skill kind and UI template."""

from __future__ import annotations

import re
from typing import Literal

ForgeCodegenProfile = Literal["headless", "interactive_builtin", "interactive_custom"]
ForgeReviseProfile = Literal["interactive_builtin", "interactive_custom"]

_BUILTIN_TEMPLATES = frozenset({"list", "calendar", "table"})


def _plan_lower(plan: str) -> str:
    return (plan or "").lower()


def infer_codegen_profile(
    plan: str,
    *,
    manifest: dict | None = None,
) -> ForgeCodegenProfile:
    """Choose codegen prompt from manifest (edit flow) or approved plan text."""
    if manifest is not None:
        kind = manifest.get("kind", "headless")
        if kind != "interactive":
            return "headless"
        ui = manifest.get("ui") or {}
        template = ui.get("template")
        if template in _BUILTIN_TEMPLATES:
            return "interactive_builtin"
        return "interactive_custom"

    text = _plan_lower(plan)
    if re.search(r"\bheadless\b", text) and not re.search(r"\binteractive\b", text):
        return "headless"
    if re.search(r"\binteractive\b", text):
        if any(
            t in text
            for t in (
                "template: list",
                "template: calendar",
                "template: table",
                "template list",
                "template calendar",
                "template table",
            )
        ):
            return "interactive_builtin"
        return "interactive_custom"
    return "headless"


def infer_revise_profile(manifest: dict | None) -> ForgeReviseProfile:
    """Choose revise-preview prompt for interactive skills."""
    ui = (manifest or {}).get("ui") or {}
    if ui.get("template") == "custom":
        return "interactive_custom"
    return "interactive_builtin"
