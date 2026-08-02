"""task_manager.py — Full to-do/task management with due dates, priorities, and status.

Adapted from agentic-os-personal-main's task system in server/index.js + server/ai/assistant.js.
Stores tasks in the shared SQLite database.
"""

from datetime import datetime
from core.data.database import get_db


def add_task(title, notes=None, due_date=None, priority=0):
    """Add a new task.

    Args:
        title: Task title/description (required)
        notes: Optional notes
        due_date: YYYY-MM-DD format (default: today)
        priority: 0 (normal), 1 (important), 2 (urgent)

    Returns:
        dict with the created task
    """
    db = get_db()
    if not due_date:
        due_date = datetime.now().strftime("%Y-%m-%d")
    task = db.add_task(title=title, notes=notes, due_date=due_date, priority=priority)
    return task


def list_tasks(filter_date=None, status=None):
    """List tasks with optional filters."""
    db = get_db()
    return db.list_tasks(due_date=filter_date, status=status)


def complete_task(task_id):
    """Mark a task as done."""
    db = get_db()
    return db.complete_task(task_id)


def complete_task_by_title(title_part):
    """Find and complete a task by matching part of its title."""
    db = get_db()
    tasks = db.list_tasks()
    query = title_part.lower().strip()
    for t in tasks:
        if query in t["title"].lower():
            return db.complete_task(t["id"])
    return None


def update_task(task_id, **kwargs):
    """Update task fields. Allowed: title, notes, due_date, priority, status."""
    db = get_db()
    allowed = {"title", "notes", "due_date", "priority", "status"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return db.get_task(task_id)
    return db.update_task(task_id, **updates)


def delete_task(task_id):
    """Delete a task."""
    db = get_db()
    return db.delete_task(task_id)


def task_manager(parameters=None, response=None, player=None, session_memory=None, speak=None):
    """Tool entry point — manage to-do tasks."""
    params = parameters or {}
    action = params.get("action", "list").strip().lower()
    title = params.get("title", "").strip()
    notes = params.get("notes", "").strip()
    due_date = params.get("due_date", "").strip()
    priority = int(params.get("priority", 0))
    task_id = params.get("task_id")
    status_filter = params.get("status", "").strip()

    db = get_db()

    if action == "add":
        if not title:
            return "Please provide a task title, Yuvan."
        if not due_date:
            due_date = datetime.now().strftime("%Y-%m-%d")
        if player:
            player.write_log("[Tasks] Adding: " + title)
        task = add_task(title, notes=notes or None, due_date=due_date, priority=priority)
        return (
            'Added task "' + title + '" due ' + due_date
            + " with priority " + str(priority) + ", Yuvan."
        )

    if action == "done" or action == "complete":
        if task_id:
            result = complete_task(task_id)
        elif title:
            result = complete_task_by_title(title)
        else:
            return "Please provide a task title or ID to complete, Yuvan."
        if result:
            return 'Task "' + result["title"] + '" marked as done, Yuvan.'
        return "Task not found, Yuvan."

    if action == "delete":
        if not task_id:
            return "Please provide a task ID to delete, Yuvan."
        if delete_task(task_id):
            return "Task deleted, Yuvan."
        return "Task not found, Yuvan."

    if action == "update":
        if not task_id:
            return "Please provide a task ID to update, Yuvan."
        updates = {}
        if title:
            updates["title"] = title
        if notes:
            updates["notes"] = notes
        if due_date:
            updates["due_date"] = due_date
        if "priority" in params:
            updates["priority"] = priority
        result = update_task(int(task_id), **updates)
        if result:
            return 'Updated task "' + result["title"] + '", Yuvan.'
        return "Task not found, Yuvan."

    # Default: list tasks
    tasks = list_tasks(
        filter_date=due_date or None,
        status=status_filter or None,
    )

    if not tasks:
        date_str = " due " + due_date if due_date else ""
        return "No tasks found" + date_str + ", Yuvan."

    lines = ["Your tasks, Yuvan:"]
    for t in tasks:
        prio_mark = {2: "!! ", 1: "! ", 0: ""}.get(t["priority"], "")
        status_str = " [DONE]" if t["status"] == "done" else ""
        due_str = " (" + t["due_date"] + ")" if t.get("due_date") else ""
        lines.append(prio_mark + t["title"] + due_str + status_str)

    return "\n".join(lines)
