"""
memory/write_gate.py — Write approval gate for memory operations.

Provides a security layer for memory writes:
  - Gate evaluates write requests: allow, block, or stage for approval
  - Memory writes can be staged with provenance metadata
  - Apply staged writes after user approval

Designed after Hermes Agent's tools/write_approval.py by Nous Research.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Gate modes
GATE_MODE_DISABLED = "disabled"  # All writes pass through (default)
GATE_MODE_BLOCK = "block"        # Block all writes
GATE_MODE_STAGE = "stage"        # Stage writes for approval

# In-memory store for staged writes
_staged_writes: Dict[str, Dict[str, Any]] = {}
_staged_writes_order: List[str] = []


@dataclass
class GateDecision:
    """Result of a gate evaluation."""
    allow: bool = True
    blocked: bool = False
    staged: bool = False
    message: str = ""


def evaluate_gate(
    action: str,
    target: str,
    content: Optional[str] = None,
    old_text: Optional[str] = None,
    mode: str = GATE_MODE_DISABLED,
) -> GateDecision:
    """
    Evaluate whether a memory write should proceed.

    Args:
        action: 'add', 'replace', 'remove'
        target: 'memory' or 'user'
        content: Entry content (for add/replace)
        old_text: Substring identifying entry (for replace/remove)
        mode: Gate mode (disabled, block, stage)

    Returns:
        GateDecision with allow/blocked/staged flags.
    """
    if mode == GATE_MODE_BLOCK:
        return GateDecision(
            allow=False,
            blocked=True,
            message=f"Memory writes are blocked (gate mode: {mode})",
        )

    if mode == GATE_MODE_STAGE:
        return GateDecision(
            allow=False,
            staged=True,
            message="Memory write staged for approval",
        )

    return GateDecision(allow=True)


def stage_write(
    action: str,
    target: str,
    content: Optional[str] = None,
    old_text: Optional[str] = None,
    origin: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Stage a memory write for later approval.

    Returns the staged record dict with a unique id.
    """
    record = {
        "id": str(uuid.uuid4())[:8],
        "action": action,
        "target": target,
        "content": content,
        "old_text": old_text,
        "origin": origin or "unknown",
        "staged_at": time.time(),
        "applied": False,
    }
    _staged_writes[record["id"]] = record
    _staged_writes_order.append(record["id"])
    logger.info("Memory write staged: %s %s/%s", action, target, record["id"])
    return record


def list_staged() -> List[Dict[str, Any]]:
    """List all staged writes that haven't been applied yet."""
    return [
        _staged_writes[wid]
        for wid in _staged_writes_order
        if wid in _staged_writes and not _staged_writes[wid].get("applied")
    ]


def approve(write_id: str) -> bool:
    """
    Approve and apply a staged write.

    Returns True if the write was approved and applied successfully.
    """
    record = _staged_writes.get(write_id)
    if not record:
        return False
    if record.get("applied"):
        return True  # Already applied

    try:
        from core.memory.memory_tool import memory_tool as mt

        result = mt(
            action=record["action"],
            target=record["target"],
            content=record.get("content"),
            old_text=record.get("old_text"),
        )
        result_data = json.loads(result)
        if result_data.get("success"):
            record["applied"] = True
            record["applied_at"] = time.time()
            logger.info("Staged write %s approved and applied", write_id)
            return True
        else:
            logger.warning("Staged write %s approval failed: %s", write_id, result_data.get("error"))
            return False
    except Exception as e:
        logger.warning("Staged write %s approval error: %s", write_id, e)
        return False


def reject(write_id: str) -> bool:
    """
    Reject a staged write (remove without applying).

    Returns True if found and removed.
    """
    record = _staged_writes.pop(write_id, None)
    if record:
        if write_id in _staged_writes_order:
            _staged_writes_order.remove(write_id)
        logger.info("Staged write %s rejected", write_id)
        return True
    return False


def clear_staged() -> int:
    """Clear all staged writes. Returns count cleared."""
    count = len(_staged_writes)
    _staged_writes.clear()
    _staged_writes_order.clear()
    if count:
        logger.info("Cleared %d staged writes", count)
    return count


def pending_count() -> int:
    """Return the number of pending (not yet applied) staged writes."""
    return sum(
        1 for r in _staged_writes.values() if not r.get("applied")
    )


def current_origin() -> str:
    """Return an identifier for the current context (e.g. 'voice', 'dashboard')."""
    return "voice"  # Default for IRA's main voice loop


# ---------------------------------------------------------------------------
# Built-in gate configuration (stores mode in persona config)
# ---------------------------------------------------------------------------

def get_gate_mode() -> str:
    """Get the current gate mode from persona config."""
    try:
        from pathlib import Path
        import sys
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
        cfg_path = base / "persona" / "persona_config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            return cfg.get("memory_gate_mode", GATE_MODE_DISABLED)
    except Exception:
        pass
    return GATE_MODE_DISABLED


def set_gate_mode(mode: str) -> bool:
    """Set the gate mode in persona config."""
    valid_modes = {GATE_MODE_DISABLED, GATE_MODE_BLOCK, GATE_MODE_STAGE}
    if mode not in valid_modes:
        return False
    try:
        from pathlib import Path
        import sys
        import json
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
        cfg_path = base / "persona" / "persona_config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        else:
            cfg = {}
        cfg["memory_gate_mode"] = mode
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        logger.info("Memory gate mode set to: %s", mode)
        return True
    except Exception as e:
        logger.warning("Failed to set gate mode: %s", e)
        return False