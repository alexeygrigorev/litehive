"""Dashboard-style status sections rendered for ``litehive workspace status``.

These helpers format the indented prose blocks (``=== Active Task ===``,
``=== Queue ===``, ``=== Recent Activity ===``, etc.) that the workspace status
command prints to operators. Health-mode renderers live in ``status_health``.
"""

import json
from typing import Any

from litehive.domain.common import TaskStatus
from litehive.domain.task import TaskRecord
from litehive.observability.status_summary import (
    _duration_label,
    _task_engine_label,
    _task_last_verdict_label,
    _task_stage_label,
)
from litehive.workspace import Workspace


def render_active_task_section(task: TaskRecord | None, default_engine: str) -> list[str]:
    """Render the Active Task dashboard section."""
    lines: list[str] = ["=== Active Task ==="]
    if task is None:
        lines.append("  (none)")
        return lines

    engine = _task_engine_label(task, default_engine)
    stage = _task_stage_label(task)

    # Task elapsed duration (since run started)
    task_duration = _duration_label(task.runtime.pipeline.run_started_at, 0)

    # Stage elapsed duration
    stage_duration = _duration_label(
        task.runtime.pipeline.current_stage.started_at,
        task.runtime.pipeline.current_stage.duration_seconds,
    )

    lines.append(f"  {task.id} {stage} with {engine}, running for {task_duration}")
    lines.append(f"  stage: {stage} elapsed {stage_duration}")
    lines.append(f"  title: {task.title}")

    return lines


def render_active_tasks_section(
    tasks: list[TaskRecord],
    default_engine: str,
) -> list[str]:
    """Render the Active Tasks dashboard section."""
    lines: list[str] = ["=== Active Tasks ==="]
    if not tasks:
        lines.append("  (none)")
        return lines

    lines.append(f"  {len(tasks)} active task(s)")
    for task in tasks:
        engine = _task_engine_label(task, default_engine)
        stage = _task_stage_label(task)
        task_duration = _duration_label(task.runtime.pipeline.run_started_at, 0)
        worktree = task.runtime.pipeline.git.worktree_path or task.git.worktree_path or "-"

        lines.append(f"  {task.id} {stage} with {engine}, running for {task_duration}")
        lines.append(f"    title: {task.title}")
        lines.append(f"    worktree: {worktree}")

    return lines


def find_last_completed_task(tasks: list[TaskRecord]) -> TaskRecord | None:
    """Return the most recently completed (done) task by updated_at."""
    done_tasks = [t for t in tasks if t.status == TaskStatus.DONE]
    if not done_tasks:
        return None
    return max(done_tasks, key=lambda t: t.updated_at or "")


def render_last_completed_section(task: TaskRecord | None, workspace: Workspace) -> list[str]:
    """Render the Last Completed dashboard section."""
    lines: list[str] = ["=== Last Completed ==="]
    if task is None:
        lines.append("  (none)")
        return lines

    outcome = task.runtime.pipeline.last_outcome
    verdict = outcome.kind or _task_last_verdict_label(task, workspace)
    when = outcome.recorded_at or task.updated_at or "-"
    lines.append(f"  {task.id} {task.title} verdict={verdict} at {when}")
    return lines


def render_queue_section(queue: list[str], tasks: list[TaskRecord]) -> list[str]:
    """Render the Queue dashboard section."""
    lines: list[str] = ["=== Queue ==="]
    count = len(queue)
    if count == 0:
        lines.append("  (empty)")
        return lines

    task_by_id = {t.id: t for t in tasks}
    head_id = queue[0]
    head_task = task_by_id.get(head_id)
    if head_task:
        head_title = head_task.title
    else:
        head_title = head_id
    lines.append(f"  {count} queued, next: {head_id} {head_title}")
    return lines


def collect_recent_activity(workspace: Workspace, limit: int = 5) -> list[dict[str, Any]]:
    """Return recent task events from SQLite."""
    with workspace.connect() as connection:
        rows = connection.execute(
            """
            SELECT payload
            FROM events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


_EVENT_LABELS: dict[str, str] = {
    "stage_completed": "stage completed",
    "stage_started": "stage started",
    "subagent_started": "subagent started",
    "subagent_completed": "subagent completed",
    "subagent_finished": "finished",
    "subagent_pid": "running",
    "subagent_progress": "progress",
    "engine_switch": "engine switched",
    "engine_fallback": "engine fallback",
    "task_queued": "task queued",
    "task_completed": "task completed",
    "task_flagged": "task flagged",
    "task_interrupted": "task interrupted",
    "execution_error": "error",
    "retry": "retry",
}


def render_recent_activity_section(events: list[dict[str, Any]]) -> list[str]:
    """Render the Recent Activity dashboard section."""
    lines: list[str] = ["=== Recent Activity ==="]
    if not events:
        lines.append("  (no recent activity)")
        return lines
    for event in events:
        kind = event.get("kind", "unknown")
        label = _EVENT_LABELS.get(kind, kind)
        ts = event.get("ts", "-")
        task_id = event.get("task_id", "-")
        data = event.get("data", {})
        detail_parts = [f"{task_id} {label}"]
        if kind in ("stage_completed", "stage_started") and data.get("stage"):
            detail_parts.append(data["stage"])
        if kind == "engine_switch" and data.get("to_engine"):
            detail_parts.append(f"-> {data['to_engine']}")
        if "role" in data:
            detail_parts.append(data["role"])
        if "engine" in data:
            detail_parts.append(data["engine"])
        if "exit_code" in data:
            detail_parts.append(f"exit={data['exit_code']}")
        if data.get("verdict"):
            detail_parts.append(f"verdict={data['verdict']}")
        lines.append(f"  [{ts}] {' '.join(detail_parts)}")
    return lines
