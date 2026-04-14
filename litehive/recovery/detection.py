"""Small recovery predicates for the live stale-runner path."""

from datetime import UTC, datetime
from pathlib import Path

from litehive.domain.task import TaskRecord
from litehive.observability.events import last_event_timestamp


def is_stranded_commit_task(task: TaskRecord) -> bool:
    return (
        task.pipeline_status == "done"
        and task.git.commit_sha is None
        and task.git.checkpoint_attempts > 0
    )


def should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == "commit_to_git" and task.status in {
        "queued",
        "in_progress",
        "interrupted",
    }


def has_inactive_running_tasks(
    root: Path,
    tasks_by_id: dict[str, TaskRecord],
    timeout_seconds: float,
) -> bool:
    for task in tasks_by_id.values():
        if task.runtime.execution_status != "running":
            continue
        ts_str = last_event_timestamp(root, task)
        if ts_str is None:
            continue
        try:
            event_time = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        if (datetime.now(UTC) - event_time).total_seconds() > timeout_seconds:
            return True
    return False
