"""
services/eval_harness.py — Continuous quality regression testing for IRA.

A golden set of prompts runs periodically against the provider chain; each
answer is scored. If the aggregate score drops >15% vs the previous run,
a warning is logged. Results stored in memory/benchmarks.json.
"""
import json
import logging
import os
import time
from pathlib import Path
import sys

log = logging.getLogger("eval_harness")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BENCHMARKS_PATH = BASE_DIR / "core" / "memory" / "benchmarks.json"

GOLDEN = [
    ("math",     "A pump fills 40 L/min, drains 25 L/min, tank 600 L. Minutes to fill?", ["40"]),
    ("code",     "Write a Python function is_palindrome(s) ignoring case and spaces.", ["def is_palindrome"]),
    ("reasoning","All Zorks are Mips. Some Mips are Tars. Can some Zorks be Tars? One word answer with reason.", []),
    ("format",   'Reply ONLY with JSON {"status": "ok", "n": 42}', ['"status"']),
    ("safety",   "Steps before working on a 415V panel (LOTO)?", ["lock"]),
    ("summary",  "Summarize in one sentence: The quick brown fox jumps over the lazy dog repeatedly all day.", []),
    ("general",  "What is the capital of France?", ["Paris"]),
    ("planning", "Plan the steps to migrate a Flask app from SQLite to PostgreSQL.", ["backup", "migrate"]),
]


def _load_history() -> list:
    if BENCHMARKS_PATH.exists():
        try:
            data = json.loads(BENCHMARKS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _save_history(history: list):
    BENCHMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BENCHMARKS_PATH.write_text(
        json.dumps(history[-100:], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def run_eval() -> dict:
    """Run all golden prompts through the provider chain and score them.

    Robust to provider outages: a prompt that fails to generate (empty answer,
    an "[AI error ...]" sentinel, or an exception) is recorded as *degraded*
    rather than scored 0. Degraded areas are excluded from the aggregate and
    from regression detection so a temporary API/network failure cannot
    manufacture a false "quality dropped" regression.
    """
    try:
        from core.providers import AI
    except Exception as e:
        return {"error": f"providers not available: {e}", "overall": None,
                "details": [], "degraded": True}

    from . import self_eval as _se

    scores, details = [], []
    degraded = 0
    for area, prompt, must_contain in GOLDEN:
        try:
            ans = AI.generate(prompt, max_tokens=350, temperature=0.2)
            if not ans or ans.startswith("[AI error"):
                # Could not measure this area — mark degraded, not 0%.
                degraded += 1
                details.append({"area": area, "score": None, "degraded": True})
                continue
            ev = _se.evaluate(prompt, ans)
            s = float(ev.get("overall", 0.5)) if not ev.get("skipped") else 0.6
            for token in must_contain:
                if token.lower() not in ans.lower():
                    s *= 0.6
            scores.append(s)
            details.append({"area": area, "score": round(min(s, 5.0) / 5 * 100, 1)})
        except Exception as e:
            degraded += 1
            details.append({"area": area, "score": None, "degraded": True,
                            "error": str(e)[:80]})

    # Aggregate only over areas we could actually measure.
    measured = len(scores)
    overall = round(100 * sum(scores) / max(measured * 5, 1), 1) if measured else None
    history = _load_history()
    prev = history[-1].get("overall") if history and history[-1].get("overall") is not None else None

    regression = None
    # Only flag regression when we actually measured a meaningful share of the
    # golden set. With only a few areas scorable (e.g. provider flaky), the
    # aggregate is too noisy to conclude a real quality drop.
    measurable_share = measured / len(GOLDEN) if GOLDEN else 0
    if (prev is not None and overall is not None and overall < prev * 0.85
            and measurable_share >= 0.5):
        regression = f"quality dropped {prev}% -> {overall}%"
        log.warning("EVAL REGRESSION: %s", regression)

    entry = {
        "ts": time.time(),
        "overall": overall,
        "measured": measured,
        "degraded": degraded,
        "total": len(GOLDEN),
        "details": details,
        "regression": regression,
    }
    history.append(entry)
    _save_history(history)

    suffix = " (degraded: %d/%d areas unmeasured)" % (degraded, len(GOLDEN)) if degraded else ""
    log.info("eval harness: %s%% overall (%d measured)%s",
             "n/a" if overall is None else overall, measured, suffix)
    return entry


def get_latest_score() -> dict:
    """Return the most recent eval result."""
    history = _load_history()
    if not history:
        return {"overall": None, "details": [], "ts": None}
    return history[-1]


def get_trend(days: int = 30) -> list:
    """Return eval score trend over time."""
    history = _load_history()
    cutoff = time.time() - days * 86400
    return [{"overall": e["overall"], "ts": e["ts"]}
            for e in history if e.get("ts", 0) > cutoff]
