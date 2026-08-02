"""
memory/threat_patterns.py — Threat pattern detection for memory content security.

Shared pattern library for prompt-injection / promptware / exfiltration detection.
Used by MemoryStore during write operations and snapshot building.

Pattern philosophy
------------------
Patterns are organized by ATTACK CLASS:
- "all" — applied everywhere (classic prompt injection, exfiltration)
- "context" — applied to context files + memory (broader detection)
- "strict" — applied to memory writes only (aggressive checks)

Pattern anchoring
-----------------
New patterns anchor on C2-specific vocabulary or unambiguous attack behavior,
NOT on bossy English. Phrases like "you are obligated to" alone are too common
in legitimate instruction-writing to flag.

Designed after Hermes Agent's tools/threat_patterns.py by Nous Research.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

# Hard cap on text scanned with regexes.
MAX_SCAN_CHARS = 65_536

# Bounded filler used between key attack words (prevents simple bypasses)
_FILLER = r"(?:\w+\s+){0,8}"

# Pattern: (compiled_regex, pattern_id, scope)
_PATTERNS: List[Tuple[re.Pattern, str, str]] = []


def _p(pattern: str, pattern_id: str, scope: str = "strict") -> None:
    """Register a pattern with case-insensitive flag."""
    _PATTERNS.append((re.compile(pattern, re.IGNORECASE | re.DOTALL), pattern_id, scope))


# ---------------------------------------------------------------------------
# Class 1 — Classic prompt injection (scope: all)
# ---------------------------------------------------------------------------

_p(r"Ignore\s+all\s+(?:prior\s+|previous\s+)?instructions", "ignore_instructions", "all")

_p(
    r"(?:Disregard|Ignore)\s+" + _FILLER + r"(?:above|prior|previous|given|system)\s+"
    r"(?:instructions|directives|prompts|commands)",
    "ignore_above_instructions",
    "all",
)

_p(
    r"Your\s+(?:new\s+)?(?:instructions?|directives?|prompts?|system\s+prompts?)\s+"
    r"(?:are|is|will\s+be|should\s+be)\s*:",
    "new_instruction_declaration",
    "all",
)

_p(
    r"Repeat\s+(?:the\s+)?(?:above|all|everything|this|the\s+following)\s+"
    + _FILLER + r"(?:text|prompt|instructions|message|code)",
    "repeat_request",
    "all",
)

# ---------------------------------------------------------------------------
# Class 2 — Exfiltration (scope: all)
# ---------------------------------------------------------------------------

_p(
    r"(?:print|output|display|send|post|email|leak|exfiltrate)\s+"
    + _FILLER + r"(?:your\s+)?(?:system\s+)?(?:prompt|instructions|directives)",
    "exfil_prompt",
    "all",
)

_p(
    r"(?:copy|forward|transmit|upload|export)\s+"
    + _FILLER + r"(?:all\s+)?(?:conversation|chat|dialogue|history|messages)",
    "exfil_conversation",
    "all",
)

# ---------------------------------------------------------------------------
# Class 3 — Promptware / C2 (scope: context)
# ---------------------------------------------------------------------------

_p(
    r"(?:you\s+are\s+(?:now|henceforth)\s+)" + _FILLER + r"(?:a\s+)?(?:free|unbounded|unshackled|unlimited)",
    "unshackle_claim",
    "context",
)

_p(
    r"(?:no\s+(?:rules?|limits?|boundaries?|restrictions?|constraints?|guardrails?|safeguards?))",
    "no_limits_claim",
    "context",
)

_p(
    r"(?:you\s+(?:can|may|should|must)\s+(?:now\s+)?(?:do|say|write|output)\s+anything)"
    + _FILLER + r"(?:you\s+)?(?:want|like|please)",
    "do_anything_claim",
    "context",
)

# ---------------------------------------------------------------------------
# Class 4 — Aggressive behavioral hijack (scope: strict)
# ---------------------------------------------------------------------------

_p(
    r"(?:Override|Ignore|Disregard|Violate|Break)\s+"
    + _FILLER + r"(?:your\s+)?(?:core\s+)?(?:values|ethics|guidelines|policy|rules|safety)",
    "override_ethics",
    "strict",
)

_p(
    r"(?:you\s+(?:have|possess)\s+(?:no\s+)?(?:ethical|moral|legal)\s+(?:obligations?|duties?|boundaries?|restrictions?))",
    "no_ethics_claim",
    "strict",
)

_p(
    r"(?:pretend|imagine|role.?play\s+as|act\s+as\s+if)\s+"
    + _FILLER + r"(?:you.?(?:are|were|can)\s+)?(?:a\s+)?(?:god|superior|omniscient|unbound)",
    "role_play_elevation",
    "strict",
)

# ---------------------------------------------------------------------------
# Scan functions
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """NFKC-normalize text for consistent matching."""
    return unicodedata.normalize("NFKC", text)


def scan_for_threats(text: str, scope: str = "all") -> List[str]:
    """
    Scan text for threat patterns at the given scope level.

    Scope resolution:
      - "all" matches patterns with scope "all"
      - "context" matches "all" + "context"
      - "strict" matches "all" + "context" + "strict"

    Returns list of matching pattern IDs (empty if clean).
    """
    if not text or not text.strip():
        return []

    text = _normalize(text)[:MAX_SCAN_CHARS]

    resolved_scopes = {"all"}
    if scope in ("context", "strict"):
        resolved_scopes.add("context")
    if scope == "strict":
        resolved_scopes.add("strict")

    findings: List[str] = []
    for pattern, pattern_id, pattern_scope in _PATTERNS:
        if pattern_scope not in resolved_scopes:
            continue
        if pattern.search(text):
            findings.append(pattern_id)

    return findings


def first_threat_message(text: str, scope: str = "strict") -> Optional[str]:
    """
    Scan text and return the first threat message, or None if clean.

    Returns a descriptive error string if threats are found, suitable for
    returning as a tool error or write rejection message.
    """
    findings = scan_for_threats(text, scope=scope)
    if not findings:
        return None

    if len(findings) == 1:
        return (
            f"This content was blocked by security scan: matched threat pattern "
            f"'{findings[0]}'. Remove or rewrite the flagged content and try again."
        )

    return (
        f"This content was blocked by security scan: matched {len(findings)} "
        f"threat patterns ({', '.join(findings)}). "
        f"Remove or rewrite the flagged content and try again."
    )