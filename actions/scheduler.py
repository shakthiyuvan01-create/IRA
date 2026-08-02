"""
scheduler.py — Scheduled automations for IRA (like Hermes' cron).

Yuvan can say "every morning at 7 brief me on AI news" and IRA will run that
prompt itself, unattended, at that time. Schedules are stored locally.

One tool `scheduler` with actions:
    add    → prompt + time("HH:MM") + repeat("daily"|"weekly"|"once") [+ day]
    list   → show all schedules
    cancel → remove by id

A background thread (start_scheduler) fires due tasks by feeding the prompt
back into IRA via the same path as a typed command.
"""
import json
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday",
             "friday", "saturday", "sunday"]


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


SCHED_PATH      = _base_dir() / "core" / "memory" / "schedules.json"
_lock           = threading.Lock()
_runner_started = False


def _load() -> list:
    try:
        return json.loads(SCHED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list) -> None:
    try:
        SCHED_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCHED_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as e:
        print(f"[Scheduler] save error: {e}")


def _norm_time(t: str) -> str | None:
    t = (t or "").strip().lower().replace(".", ":")
    # Accept "7", "7:00", "07:00", "7am", "7:30 pm"
    ampm = ""
    if t.endswith("am") or t.endswith("pm"):
        ampm = t[-2:]
        t = t[:-2].strip()
    if ":" not in t:
        t = t + ":00"
    try:
        h, m = t.split(":")[:2]
        h, m = int(h), int(m)
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except Exception:
        pass
    return None


def scheduler(parameters=None, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "list").strip().lower()

    if action in ("add", "create", "new"):
        prompt = (params.get("prompt") or params.get("task") or params.get("text") or "").strip()
        if not prompt:
            return "What should I do on schedule, Yuvan?"
        t = _norm_time(params.get("time", ""))
        if not t:
            return "What time should I run it, Yuvan? (e.g. 07:00)"
        repeat = (params.get("repeat") or "daily").strip().lower()
        day    = (params.get("day") or "").strip().lower()
        if repeat == "weekly" and day not in _WEEKDAYS:
            return "For a weekly task, tell me the weekday, Yuvan (e.g. Monday)."
        item = {
            "id":       uuid.uuid4().hex[:6],
            "prompt":   prompt,
            "time":     t,
            "repeat":   repeat if repeat in ("daily", "weekly", "once") else "daily",
            "day":      day,
            "last_run": "",
        }
        with _lock:
            items = _load()
            items.append(item)
            _save(items)
        when = f"every day at {t}" if item["repeat"] == "daily" else \
               f"every {day.title()} at {t}" if item["repeat"] == "weekly" else \
               f"once at {t}"
        return f"Scheduled, Yuvan: I'll {prompt} {when}. (id {item['id']})"

    if action in ("list", "show"):
        items = _load()
        if not items:
            return "You have no scheduled tasks, Yuvan."
        lines = []
        for it in items:
            when = it["time"] + (" daily" if it["repeat"] == "daily"
                                 else f" {it['day'].title()}" if it["repeat"] == "weekly"
                                 else " once")
            lines.append(f"[{it['id']}] {when} — {it['prompt']}")
        return "Your scheduled tasks, Yuvan:\n" + "\n".join(lines)

    if action in ("cancel", "remove", "delete"):
        sid = str(params.get("id") or "").strip().lower()
        with _lock:
            items = _load()
            new   = [i for i in items if i["id"].lower() != sid]
            if len(new) == len(items):
                return "I couldn't find a schedule with that id, Yuvan."
            _save(new)
        return f"Cancelled schedule {sid}, Yuvan."

    return "Scheduler actions, Yuvan: add (prompt+time), list, cancel (id)."


def start_scheduler(on_command) -> None:
    """Background runner: fires due tasks by calling on_command(prompt)."""
    global _runner_started
    if _runner_started:
        return
    _runner_started = True

    def _loop():
        print("[Scheduler] runner started.")
        while True:
            try:
                now      = datetime.now()
                hhmm     = now.strftime("%H:%M")
                today    = now.strftime("%Y-%m-%d")
                weekday  = _WEEKDAYS[now.weekday()]
                changed  = False
                with _lock:
                    items = _load()
                    keep  = []
                    due   = []
                    for it in items:
                        fire = (it.get("time") == hhmm and it.get("last_run") != today)
                        if fire and it.get("repeat") == "weekly" and it.get("day") != weekday:
                            fire = False
                        if fire:
                            it["last_run"] = today
                            due.append(it)
                            changed = True
                            if it.get("repeat") == "once":
                                continue   # drop one-shot after firing
                        keep.append(it)
                    if changed:
                        _save(keep)
                for it in due:
                    try:
                        print(f"[Scheduler] firing {it['id']}: {it['prompt']}")
                        on_command(it["prompt"])
                    except Exception as e:
                        print(f"[Scheduler] fire error: {e}")
            except Exception as e:
                print(f"[Scheduler] loop error: {e}")
            time.sleep(30)

    threading.Thread(target=_loop, daemon=True).start()
