"""Routes spoken or typed "improve ..." / "add a ... skill" requests to the
Self Improve panel, where IRA shows the code change before applying it."""
from __future__ import annotations


def handler(parameters=None, player=None):
    request = str((parameters or {}).get("request", "")).strip()
    if not request:
        return "Tell me what to improve or which skill to add."
    from core.self_improve import panel
    if not panel.submit(request):
        return "The Self Improve panel is not available in this window."
    return ("I'm working on that in the Self Improve panel. I'll show you the exact "
            "code change there, and nothing changes until you press Apply.")


TOOL = {
    "name": "self_improve",
    "description": (
        "Change IRA's own code. Use when the user says 'improve ...' about IRA, or asks "
        "IRA to add, create or learn a new skill, feature, ability or command (for example "
        "'add a writing skill', 'add an opening skill'). Pass the user's request verbatim."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "request": {"type": "STRING",
                        "description": "The user's improvement or new-skill request, verbatim."},
        },
        "required": ["request"],
    },
    "handler": handler,
}
