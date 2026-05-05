"""Engine switching: move a task to a different agent engine for the next run.

The flow is operator-initiated (``litehive queue switch``):

1. Stop the runner if it's currently working on this task.
2. Mark the engine switch on the task's runtime metadata.
3. Re-queue the task at the front so it picks up on the new engine.
4. Append an activity entry + audit entry naming the previous and
   new engines and the prior subagent artifacts the new engine can
   consult.

Lives in its own module rather than in ``tasks.status`` because it
is one self-contained operator action with its own audit shape.
"""

from pathlib import Path

from litehive.config.loading import load_config
from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.reports import TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import SwitchTaskSummary
from litehive.state.records import get_task_record, require_task
from litehive.state.persist import load_state
from litehive.tasks.activity import append_task_activity
from litehive.tasks.audit import (
    append_task_audit_entries,
    build_task_audit_entry,
    snapshot_task_audit_state,
)
from litehive.tasks.constants import CLOSED_TASK_STATUSES, VALID_TASK_ENGINES
from litehive.tasks.paths import latest_subagent_base, task_dir
from litehive.tasks.queue import move_queued_task
from litehive.tasks.runtime import mark_engine_switch
from litehive.workspace import Workspace


def _effective_task_engine(root: Path, task: TaskRecord) -> str:
    """
    Determine which engine the task is currently running under.

    Checks the active subagent first, then the most recent subagent
    reference, then the workspace default; the result is what the audit
    and activity entries record as the "previous engine" of the switch
    so an operator reading the timeline sees the actual swap, not just
    the configured-at-task-creation engine.
    """
    if task.runtime.execution.active_subagent is not None:
        return task.runtime.execution.active_subagent.engine
    if task.subagents:
        return task.subagents[-1].engine
    return load_config(root).default_engine


def _switch_prior_work_paths(root: Path, task: TaskRecord) -> list[str]:
    """
    Collect the relative subagent artifact directories from prior runs.

    Newest-first, deduplicated, with the latest base directory pinned so
    the activity entry tells the new engine exactly where to find the
    previous engine's output. Without these paths, the engine swap would
    silently drop the prior context and the new engine would start from
    scratch.
    """
    paths: list[str] = []
    for candidate in (ref.path for ref in reversed(task.subagents)):
        if candidate and candidate not in paths:
            paths.append(candidate)
    base = latest_subagent_base(root, task)
    if base is not None:
        rel_path = str(base.relative_to(task_dir(root, task)))
        if rel_path not in paths:
            paths.append(rel_path)
    return paths


def _switch_activity_entry_message(
    task: TaskRecord,
    reason: str,
    previous_engine: str,
    new_engine: str,
    prior_work_paths: list[str],
) -> str:
    """
    Format the multi-line activity entry that follows an engine switch.

    The explicit ``prior_work`` block tells the next agent which subagent
    directories to read so the switch does not lose context; the message
    is what the new agent reads first when it picks up the task.
    """
    lines = [
        f"Engine switch requested: {reason}",
        f"engine: {previous_engine} -> {new_engine}",
        f"resume_from: {task.pipeline_status}",
    ]
    if prior_work_paths:
        lines.append("prior_work:")
        lines.extend(f"- {path}" for path in prior_work_paths)
    else:
        lines.append("prior_work: no prior subagent artifacts recorded")
    return "\n".join(lines)


def switch_task_engine(
    root: Path,
    task_id: str,
    engine: str,
    reason: str,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> SwitchTaskSummary:
    """
    Carry out an operator-initiated engine swap end-to-end.

    Stops the runner if needed, marks the switch on runtime metadata,
    re-queues the task at the front, and appends the activity + audit
    entries that name the previous and new engines plus the prior
    subagent artifacts the new engine should consult. Called by
    ``litehive queue switch``.
    """
    # inline: tasks.status imports tasks.switch_engine indirectly via
    # the queue CLI re-export; keeping these imports inside the
    # function avoids the partial-init cycle that would otherwise
    # appear when tasks.status is imported during ``litehive
    # queue switch`` startup.
    from litehive.tasks.status import resume_task, stop_current_task  # noqa: PLC0415

    if engine not in VALID_TASK_ENGINES:
        raise ValueError(f"Unsupported engine '{engine}'")
    if not reason.strip():
        raise ValueError("Switch reason must not be empty")

    task = require_task(root, task_id)
    before_task = snapshot_task_audit_state(task)
    if task.pipeline_status == PipelineStatus.DONE:
        raise ValueError(f"Task {task.id} is already done")
    if task.pipeline_status == PipelineStatus.BACKLOG:
        raise ValueError(f"Task {task.id} is still in backlog and has no runnable stage to resume")

    state = load_state(root)
    was_active = state.active_task_id == task_id
    runner_pid: int | None = None
    signal_sent = False
    if was_active:
        stop_summary = stop_current_task(root)
        task = stop_summary.task
        runner_pid = stop_summary.runner_pid
        signal_sent = stop_summary.signal_sent
    else:
        task = get_task_record(root, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")

    previous_engine = _effective_task_engine(root, task)
    mark_engine_switch(
        root,
        task,
        stage=task.pipeline_status,
        from_engine=previous_engine,
        to_engine=engine,
        reason=reason.strip(),
    )
    task = require_task(root, task.id)

    if task.status == TaskStatus.QUEUED:
        move_queued_task(root, task.id, 1)
        task = require_task(root, task.id)
    elif task.status in {TaskStatus.INTERRUPTED, TaskStatus.PARKED, TaskStatus.FLAGGED, *CLOSED_TASK_STATUSES}:
        task = resume_task(root, task.id, front=True)
    else:
        raise ValueError(f"Task {task.id} is {task.status} and cannot be switched into a queued runnable state")

    prior_work_paths = _switch_prior_work_paths(root, task)

    workspace = Workspace.from_path(root)
    append_task_activity(
        workspace,
        task,
        TaskActivityEntry(
            role="operator",
            stage=task.pipeline_status,
            verdict="comment",
            message=_switch_activity_entry_message(
                task,
                reason=reason.strip(),
                previous_engine=previous_engine,
                new_engine=engine,
                prior_work_paths=prior_work_paths,
            ),
        ),
    )
    append_task_audit_entries(
        workspace,
        [
            build_task_audit_entry(
                task_id=task.id,
                action="engine_switched",
                actor=audit_actor,
                source=audit_source,
                before_task=before_task,
                after_task=task,
                context={
                    "old_value": previous_engine,
                    "new_value": engine,
                    "from_engine": previous_engine,
                    "to_engine": engine,
                    "reason": reason.strip(),
                    "prior_work_paths": prior_work_paths,
                    "was_active": was_active,
                },
            )
        ],
    )
    return SwitchTaskSummary(
        task=task,
        previous_engine=previous_engine,
        new_engine=engine,
        was_active=was_active,
        runner_pid=runner_pid,
        signal_sent=signal_sent,
        prior_work_paths=prior_work_paths,
    )
