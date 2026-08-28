"""
services/self_optimize.py — Verified self-improvement via system prompt tuning.

The only kind that works: measure, change something safe and reversible,
measure again, keep ONLY if the score genuinely improved.

What it tunes: a "system overlay" — an extra instruction block prepended to
every chat's system prompt. It does NOT touch source code, weights, or config.

Loop:
  1. baseline = eval_harness score
  2. AI proposes an overlay targeting the weakest area
  3. apply overlay -> re-run eval
  4. improved by margin? keep it. else revert to previous overlay.
  5. log the attempt

Safeguards: off by default, overlay length capped, full rollback.
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("services.self_optimize")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = BASE_DIR / "core" / "persona" / "persona_config.json"
LOG_PATH = BASE_DIR / "core" / "memory" / "optimize_log.json"

MARGIN = 3          # must beat baseline by >= 3 points to be kept
MAX_OVERLAY = 600   # chars


def _get_overlay() -> str:
    """Read the current system overlay from persona config."""
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return cfg.get("system_overlay", "")
    except Exception:
        pass
    return ""


def _set_overlay(text: str):
    """Write a new system overlay to persona config."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg = {}
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg["system_overlay"] = text[:MAX_OVERLAY]
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("overlay write failed: %s", e)


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
        json.dumps(entries[-100:], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def run_cycle(force: bool = False) -> Dict[str, Any]:
    """Run one measure->change->verify->keep-or-revert cycle."""
    # Rate limit: once per 7 days unless forced
    log_entries = _load_log()
    if log_entries and not force:
        last = log_entries[-1].get("ts", 0)
        if time.time() - last < 7 * 86400:
            return {"skipped": True, "reason": "rate limited (7 days between cycles)"}

    try:
        from core.services.eval_harness import run_eval
    except Exception as e:
        return {"error": f"eval harness not available: {e}"}

    prev_overlay = _get_overlay()

    # 1. Baseline
    base = run_eval()
    baseline = base.get("overall", 0)
    details = base.get("details", [])
    if not details:
        return {"error": "eval returned no details"}

    # Only scored (non-degraded) areas are comparable; degraded ones are
    # unmeasured, not "weak". Guard against an all-degraded result.
    scored = [d for d in details if d.get("score") is not None]
    if not scored:
        return {"error": "eval returned no measurable areas (provider degraded)"}
    weakest = min(scored, key=lambda d: d["score"])
    weakest_area = weakest["area"]
    weakest_score = weakest["score"]

    if baseline >= 95:
        return {"ok": True, "note": "already performing well, skipping cycle",
                "baseline": baseline}

    # 2. AI proposes improvement overlay
    try:
        from core.providers import AI
        proposal = AI.generate(
            "You tune an AI assistant's system prompt. Its weakest area is '%s' "
            "(golden-set score %d%%). Write a SHORT instruction block (max 3 "
            "sentences) to prepend to its system prompt that would improve answers "
            "in that area without hurting others. Output ONLY the instruction text."
            % (weakest_area, weakest_score),
            model="gpt-4o-mini", max_tokens=150, temperature=0.4)
    except Exception as e:
        return {"error": f"proposal failed: {e}"}

    if not proposal or proposal.startswith("[AI error"):
        return {"error": "no proposal generated"}

    candidate_overlay = (prev_overlay + "\n" + proposal.strip())[:MAX_OVERLAY].strip()

    # 3. Apply and re-measure
    _set_overlay(candidate_overlay)
    cand = run_eval()
    candidate = cand.get("overall", 0)

    # 4. Keep or revert
    kept = candidate >= baseline + MARGIN
    if not kept:
        _set_overlay(prev_overlay)  # rollback

    entry = {
        "ts": time.time(),
        "baseline": baseline,
        "candidate": candidate,
        "kept": kept,
        "target": weakest_area,
        "proposal": proposal.strip()[:MAX_OVERLAY],
    }
    log_entries.append(entry)
    _save_log(log_entries)

    log.info("self-optimize: %d%% -> %d%% targeting %s -> %s",
             baseline, candidate, weakest_area, "KEPT" if kept else "reverted")

    return {
        "baseline": baseline,
        "candidate": candidate,
        "target": weakest_area,
        "kept": kept,
        "proposal": proposal.strip(),
        "note": ("improvement verified and applied" if kept
                 else "no verified gain - reverted to previous prompt"),
    }


def history(limit: int = 20) -> list:
    """Return recent optimization attempts."""
    return _load_log()[-limit:]


def reset():
    """Wipe the overlay — return to the stock prompt."""
    _set_overlay("")
    return {"ok": True, "overlay": "cleared"}
