"""Operator-initiated task stop flow.

``litehive queue stop`` calls into ``stop_current_task`` to interrupt
the active task: signal the runner if one is alive, park the task at
its current stage so a later resume picks up where it left off, and
remove the task from the active runtime markers without leaving the
queue in an inconsistent state.

Lives in its own module rather than in ``tasks.status`` because the
flow has its own contract (StopTaskSummary, runner-pid signalling
semantics, dirty-runner recovery as a side effect) and ``status``
already owns enough.
"""

import os
import signal
import time
from pathlib import Path

from litehive.domain.common import PipelineStatus, TaskStage, TaskStatus, utcnow
from litehive.domain.runtime import RuntimeInterruptionState
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.domain.task_ops import StopTaskSummary, WorkspaceConflictError
from litehive.recovery.execution_recovery import recover_stale_runner_state
from litehive.state.locking import (
    read_runner_lock_metadata,
    runner_lock_is_held,
    runner_pid_is_alive,
    workspace_lock,
)
from litehive.state.persist import load_state, persist_task_and_state_without_runner_guard
from litehive.state.records import get_task_record, require_task
from litehive.tasks._process_signals import terminate_subagent_pid
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue import active_task_markers, validate_single_active_task


def _active_task_id_for_stop(root: Path, state: WorkspaceState) -> str:
    markers = active_task_markers(root, state)
    if not markers:
        raise ValueError("No active task to stop")
    if len(markers) > 1:
        validate_single_active_task(root, state)
    return next(iter(sorted(markers)))


def _stop_active_task_without_runner_guard(root: Path, task_id: str) -> TaskRecord:
    with workspace_lock(root):
        state = load_state(root)
        active_task_id = _active_task_id_for_stop(root, state)
        if active_task_id != task_id:
            raise WorkspaceConflictError(f"task {task_id} is no longer the active task in this workspace")
        task = get_task_record(root, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        queue_before = list(state.queue)
        if task.pipeline_status == PipelineStatus.DONE:
            raise ValueError(f"Task {task.id} is already done")
        stage = task.runtime.pipeline.current_stage.stage or task.pipeline_status

        # Park the task - this is intentional operator action, not system interruption
        now = utcnow()
        task.status = TaskStatus.PARKED
        task.runtime.pipeline.execution_status = "idle"
        task.runtime.pipeline.run_started_at = None
        task.runtime.pipeline.updated_at = now
        task.runtime.execution.active_subagent = None

        # Set minimal interruption metadata for resume functionality
        # Use "operator" source to distinguish from system interruptions
        task.runtime.execution.interruption = RuntimeInterruptionState(
            source="runner",  # CLI command execution context
            stage=stage,
            resume_stage=stage,
            pipeline_status=task.pipeline_status,
            reason=f"Task parked via CLI command from {stage} stage",
            summary=f"Task execution parked via `litehive queue stop`. Resume from `{stage}`.",
            interrupted_at=now,
        )

        # Special case: tasks at commit_to_git stage remain queued instead of parked
        if stage == TaskStage.COMMIT_TO_GIT:
            task.status = TaskStatus.QUEUED

        # Remove from active/queue state
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]

        # Re-add to queue front if remaining queued
        if task.status == TaskStatus.QUEUED and task.pipeline_status != PipelineStatus.DONE:
            state.queue.insert(0, task.id)

        journal_message = f"Task execution stopped via CLI from `{stage}` stage. Status: {task.status}."
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=journal_message,
            audit_entries=[
                build_task_audit_entry(
                    task_id=task.id,
                    action="stopped",
                    actor="operator",
                    source="cli",
                    before_task=before_task,
                    after_task=task,
                    before_queue=queue_before,
                    after_queue=state.queue,
                    context={"stage": stage, "resulting_status": task.status},
                )
            ],
        )
        return task


def stop_current_task(
    root: Path,
    *,
    wait_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
) -> StopTaskSummary:
    state = load_state(root)
    try:
        active_task_id = _active_task_id_for_stop(root, state)
    except ValueError:
        metadata = read_runner_lock_metadata(root)
        if runner_lock_is_held(root) and metadata.active_task_id:
            active_task_id = metadata.active_task_id
        else:
            raise
    runner_pid: int | None = None
    if runner_lock_is_held(root):
        deadline = time.monotonic() + max(wait_timeout_seconds, 0.0)
        sleep_interval = max(poll_interval_seconds, 0.01)
        metadata = read_runner_lock_metadata(root)
        pid = metadata.pid
        while runner_lock_is_held(root) and not runner_pid_is_alive(pid) and time.monotonic() < deadline:
            time.sleep(sleep_interval)
            metadata = read_runner_lock_metadata(root)
            pid = metadata.pid
        if runner_pid_is_alive(pid):
            runner_pid = int(pid)
            os.kill(runner_pid, signal.SIGINT)
            deadline = time.monotonic() + max(wait_timeout_seconds, 0.0)
            while runner_lock_is_held(root) and time.monotonic() < deadline:
                time.sleep(sleep_interval)
            if runner_lock_is_held(root):
                raise WorkspaceConflictError(
                    f"runner for task {active_task_id} did not stop cleanly after SIGINT (pid={runner_pid})"
                )
            if runner_pid_is_alive(runner_pid):
                settle_deadline = time.monotonic() + max(wait_timeout_seconds, 0.0)
                while runner_pid_is_alive(runner_pid) and time.monotonic() < settle_deadline:
                    time.sleep(sleep_interval)
                if runner_pid_is_alive(runner_pid):
                    terminate_subagent_pid(
                        active_task_id,
                        runner_pid,
                        wait_timeout_seconds=wait_timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                    )
            recover_stale_runner_state(root)
            state = load_state(root)
            markers = active_task_markers(root, state)
            if active_task_id not in markers:
                return StopTaskSummary(
                    task=require_task(root, active_task_id),
                    runner_pid=runner_pid,
                    signal_sent=True,
                )
        else:
            raise WorkspaceConflictError(
                f"runner for task {active_task_id} is active but has no live PID to signal cleanly"
            )

    task = _stop_active_task_without_runner_guard(root, active_task_id)
    return StopTaskSummary(task=task, runner_pid=runner_pid, signal_sent=runner_pid is not None)
