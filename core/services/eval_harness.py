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
    """Run all golden prompts through the provider chain and score them."""
    try:
        from core.providers import AI
    except Exception as e:
        return {"error": f"providers not available: {e}", "overall": 0, "details": []}

    from . import self_eval as _se

    scores, details = [], []
    for area, prompt, must_contain in GOLDEN:
        try:
            ans = AI.generate(prompt, max_tokens=350, temperature=0.2)
            s = 0.0
            if ans and not ans.startswith("[AI error"):
                ev = _se.evaluate(prompt, ans)
                s = float(ev.get("overall", 0.5)) if not ev.get("skipped") else 0.6
                for token in must_contain:
                    if token.lower() not in ans.lower():
                        s *= 0.6
            scores.append(s)
            details.append({"area": area, "score": round(min(s, 5.0) / 5 * 100, 1)})
        except Exception as e:
            scores.append(0.0)
            details.append({"area": area, "score": 0, "error": str(e)[:80]})

    overall = round(100 * sum(scores) / max(len(scores) * 5, 1), 1)
    history = _load_history()
    prev = history[-1]["overall"] if history else None

    regression = None
    if prev and overall < prev * 0.85:
        regression = f"quality dropped {prev}% -> {overall}%"
        log.warning("EVAL REGRESSION: %s", regression)

    entry = {
        "ts": time.time(),
        "overall": overall,
        "details": details,
        "regression": regression,
    }
    history.append(entry)
    _save_history(history)

    log.info("eval harness: %d%% overall%s", overall,
             f" (REGRESSION)" if regression else "")
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
