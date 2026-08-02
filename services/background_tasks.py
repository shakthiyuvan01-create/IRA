"""
services/background_tasks.py — Background task orchestrator for IRA.

Manages periodic self-improvement tasks:
  - Heartbeat: maintenance loop (30 min default)
  - Eval harness: quality checks (24h)
  - Self-optimize: prompt tuning (7 days)
  - Self-improve: failure review (20h)

All tasks run as asyncio background tasks and gracefully handle failures.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    pass

log = logging.getLogger("background_tasks")


class BackgroundTaskManager:
    """Orchestrates periodic self-improvement tasks for IRA."""

    def __init__(self, jarvis=None, ui=None):
        self.jarvis = jarvis
        self.ui = ui
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._start_time = time.time()

    async def start(self):
        """Start all background task loops."""
        if self._running:
            return
        self._running = True
        self._start_time = time.time()

        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._eval_loop()))
        self._tasks.append(asyncio.create_task(self._optimize_loop()))
        self._tasks.append(asyncio.create_task(self._improve_loop()))
        self._tasks.append(asyncio.create_task(self._curator_loop()))

        self._log("SYS: Background tasks started (heartbeat, eval, optimize, improve, curator)")

    async def stop(self):
        """Stop all background tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._log("SYS: Background tasks stopped")

    # ── Heartbeat loop ────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Run heartbeat every 30 minutes (only if active)."""
        await asyncio.sleep(120)  # Initial delay — let system settle
        while self._running:
            try:
                from services.heartbeat import supervisor as hb_super
                # Check if enough time passed and run if so
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, hb_super)
                if result and isinstance(result, dict) and result.get("updated"):
                    self._log("SYS: Heartbeat updated MEMORY.md")
            except Exception as e:
                log.debug("heartbeat loop: %s", e)
            # Sleep 15 minutes between checks (supervisor enforces actual interval)
            await asyncio.sleep(900)

    # ── Eval loop ──────────────────────────────────────────────────────────

    async def _eval_loop(self):
        """Run eval harness every 24 hours."""
        await asyncio.sleep(300)  # Initial delay
        # Run one eval on startup to establish baseline
        try:
            from services.eval_harness import run_eval
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, run_eval)
            if result and not result.get("error"):
                overall = result.get("overall", 0)
                self._log(f"SYS: Eval baseline established: {overall}%")
        except Exception as e:
            log.debug("startup eval: %s", e)

        while self._running:
            await asyncio.sleep(86400)  # 24 hours
            try:
                from services.eval_harness import run_eval
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, run_eval)
                if result:
                    overall = result.get("overall", 0)
                    regression = result.get("regression")
                    msg = f"SYS: Eval result: {overall}%"
                    if regression:
                        msg += f" ⚠️ {regression}"
                    self._log(msg)
            except Exception as e:
                log.debug("eval loop: %s", e)

    # ── Self-optimize loop ────────────────────────────────────────────────

    async def _optimize_loop(self):
        """Run self-optimization cycle every 7 days."""
        # Wait a few hours after startup before first run
        await asyncio.sleep(7200)  # 2 hours
        while self._running:
            try:
                from services.self_optimize import run_cycle
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, run_cycle)
                if result and result.get("kept"):
                    self._log(f"SYS: Self-optimize improved {result.get('target')}: "
                              f"{result.get('baseline')}% -> {result.get('candidate')}%")
                elif result and "error" not in result and not result.get("skipped"):
                    self._log(f"SYS: Self-optimize tried ({result.get('target')}) "
                              f"but no gain — reverted")
            except Exception as e:
                log.debug("optimize loop: %s", e)
            await asyncio.sleep(604800)  # 7 days

    # ── Self-improve review loop ──────────────────────────────────────────

    async def _improve_loop(self):
        """Run self-improvement review every 20 hours."""
        await asyncio.sleep(3600)  # 1 hour initial delay
        while self._running:
            try:
                from services.self_improve import run_review
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, run_review)
                if result and result.get("ok") and not result.get("skipped"):
                    suggestions = (result.get("suggestions") or "")[:100]
                    self._log(f"SYS: Self-improvement review complete: {suggestions}")
            except Exception as e:
                log.debug("improve loop: %s", e)
            await asyncio.sleep(72000)  # 20 hours

    # ── Curator loop ─────────────────────────────────────────────────────────

    async def _curator_loop(self):
        """Run memory curator every 7 days (checks hourly if conditions met)."""
        await asyncio.sleep(3600)  # 1 hour initial delay
        while self._running:
            try:
                from services.curator import maybe_run
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, maybe_run)
                if result.get("archived", 0) > 0:
                    self._log(f"SYS: Curator archived {result['archived']} stale entries")
                elif result.get("skipped") and result.get("reason"):
                    pass  # Normal — not time yet
                elif result.get("reviewed", 0) > 0:
                    self._log(f"SYS: Curator reviewed {result['reviewed']} entries, no stale found")
            except Exception as e:
                log.debug("curator loop: %s", e)
            await asyncio.sleep(3600)  # Check hourly

    # ── Manual triggers ───────────────────────────────────────────────────

    async def trigger_heartbeat(self) -> dict:
        """Manually trigger a heartbeat pass."""
        from services.heartbeat import run_tick
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_tick, True)
        if result.get("updated"):
            self._log("SYS: Heartbeat (manual) updated MEMORY.md")
        return result

    async def trigger_eval(self) -> dict:
        """Manually trigger an eval harness run."""
        from services.eval_harness import run_eval
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_eval)
        if result and not result.get("error"):
            self._log(f"SYS: Eval (manual): {result.get('overall')}%")
        return result

    async def trigger_optimize(self) -> dict:
        """Manually trigger a self-optimization cycle."""
        from services.self_optimize import run_cycle
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_cycle, True)
        if result.get("kept"):
            self._log(f"SYS: Self-optimize (manual) improved: "
                      f"{result.get('baseline')}% -> {result.get('candidate')}%")
        return result

    # ── Helpers ───────────────────────────────────────────────────────────

    def _log(self, msg: str):
        """Write a log message to the UI."""
        log.info(msg)
        if self.ui:
            try:
                self.ui.write_log(msg)
            except Exception:
                pass

    @property
    def uptime(self) -> float:
        return time.time() - self._start_time
