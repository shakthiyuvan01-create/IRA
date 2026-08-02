"""
services/self_improve.py — safeguarded self-improvement review (ported from Aurum).

Periodic job that:
  1. Reviews recent failures: failed task history, error events, slow tools
  2. Asks the AI for concrete improvement suggestions
  3. Stores suggestions in a review log — NOTHING is auto-applied

Safeguards:
  - OFF by default (requires explicit enable)
  - Suggestions only: never edits code, configs, or permissions
  - Rate-limited to one run per 20 hours
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("services.self_improve")

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_PATH = BASE_DIR / "memory" / "self_improve_log.json"
MIN_INTERVAL_S = 20 * 3600


def _load_log() -> list:
    if LOG_PATH.exists():
        try:
            data = json.loads(LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _save_log(entries: list):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(
        json.dumps(entries[-50:], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _gather_evidence() -> str:
    """Gather recent failures, errors, and slow operations from logs."""
    parts = []

    # Check chat_history for error patterns
    chat_log = BASE_DIR / "memory" / "chat_history.jsonl"
    if chat_log.exists():
        try:
            lines = chat_log.read_text(encoding="utf-8").splitlines()
            errors = [l for l in lines[-100:] if "error" in l.lower() or "fail" in l.lower() or "exception" in l.lower()]
            if errors:
                parts.append(f"Recent error/failure lines in chat log: {len(errors)}")
                for e in errors[-5:]:
                    try:
                        r = json.loads(e)
                        parts.append(f"  - {r.get('role','?')}: {r.get('text','')[:120]}")
                    except Exception:
                        parts.append(f"  - {e[:120]}")
        except Exception as e:
            log.debug("error evidence: %s", e)

    # Check benchmarks for low scores
    bench_path = BASE_DIR / "memory" / "benchmarks.json"
    if bench_path.exists():
        try:
            data = json.loads(bench_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                latest = data[-1]
                low_areas = [d for d in latest.get("details", []) if d.get("score", 100) < 60]
                if low_areas:
                    parts.append("Low eval scores:")
                    for a in low_areas:
                        parts.append(f"  - {a['area']}: {a['score']}%")
        except Exception:
            pass

    return "\n".join(parts) or "No failures or issues recorded recently."


def run_review(force: bool = False) -> Dict[str, Any]:
    """Run a self-improvement review cycle."""
    # Rate limit check
    log_entries = _load_log()
    if log_entries and not force:
        last = log_entries[-1].get("ts", 0)
        if time.time() - last < MIN_INTERVAL_S:
            return {"skipped": True, "reason": "rate limited (20h between reviews)"}

    evidence = _gather_evidence()
    if not evidence or evidence == "No failures or issues recorded recently.":
        return {"skipped": True, "reason": "no issues to review"}

    try:
        from providers import AI
        suggestions = AI.generate(
            "You are reviewing an AI assistant's recent performance. "
            "Based on the evidence below, list at most 5 concrete, safe improvement "
            "suggestions (prompts to tune, tools to make async, retries to add). "
            "Do NOT suggest code changes you cannot see. Plain text, numbered.\n\nEVIDENCE:\n"
            + evidence,
            system="You produce cautious, actionable engineering suggestions only.",
            model="gpt-4o-mini", max_tokens=600, temperature=0.2)

        log_entries.append({
            "ts": time.time(),
            "findings": evidence[:2000],
            "suggestions": suggestions[:2000],
        })
        _save_log(log_entries)

        log.info("self-improvement review stored")
        return {"ok": True, "findings": evidence[:200],
                "suggestions": suggestions[:200]}
    except Exception as e:
        log.error("self-improvement review failed: %s", e)
        return {"error": str(e)}


def get_reports(limit: int = 10) -> list:
    """Return recent self-improvement review reports."""
    entries = _load_log()
    return entries[-limit:]
