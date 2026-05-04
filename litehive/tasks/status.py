"""Task status transitions: requeue, resume, abandon, close, park, update, stop, switch."""

import os
import signal
import threading
import time
from pathlib import Path

from litehive.config.loading import load_config
from litehive.git.ops import GitError, current_head, path_differs_at_ref
from litehive.domain.common import PipelineStatus, TaskStage
from litehive.domain.task import TaskRecord, WorkspaceState

from litehive.tasks.constants import (
    CLOSED_TASK_STATUSES,
    RESUMABLE_TASK_STATUSES,
    VALID_TASK_ENGINES,
    VALID_TASK_PRIORITIES,
    RUNNER_LOCKS,
    RUNNER_LOCKS_MUTEX,
)
from litehive.domain.reports import TaskActivityEntry
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.recovery.execution_recovery import recover_stale_runner_state
from litehive.state.locking import (
    ensure_future_task_mutation_allowed,
    persist_future_task_update,
    read_runner_lock_metadata,
    runner_lock_is_held,
    runner_pid_is_alive,
    workspace_lock,
)
from litehive.state.persist import load_state, persist_task_and_state_without_runner_guard
from litehive.state.records import get_task_record, require_task
from litehive.tasks.activity import (
    append_task_activity,
    load_task_activity,
    save_task_activity,
)
from litehive.tasks.activity_rendering import (
    is_retractable_pass_entry,
    normalized_files_changed,
    retract_activity_entry,
)
from litehive.tasks.audit import (
    TaskAuditState,
    append_task_audit_entries,
    build_task_audit_entry,
    snapshot_task_audit_state,
)
from litehive.tasks.failed_runs import (
    blocking_failed_run_records,
    failed_run_block_message,
    mark_failed_run_operator_override,
)
from litehive.domain.task_ops import StopTaskSummary, SwitchTaskSummary, WorkspaceConflictError
from litehive.tasks.normalization import (
    missing_acceptance_criteria_reason,
    normalize_acceptance_criteria,
    normalize_task_text_list,
    reroute_stage_for_acceptance_criteria,
    implementation_entry_stage,
)
from litehive.tasks.paths import latest_subagent_base, task_dir
from litehive.tasks.queue import (
    active_task_markers,
    drop_task_from_workspace_state,
    move_queued_task,
    reset_task_for_recovery,
    resumable_queue_stage,
    validate_single_active_task,
    validate_task_dependencies,
)
from litehive.tasks.runtime import apply_task_outcome, clear_task_run_activity, mark_engine_switch
from litehive.worktree import resolve_recorded_worktree_path


class TaskTransitionService:
    """Application service that owns task lifecycle transitions."""

    def __init__(self, root: Path):
        self.root = root

    def close(
        self,
        task_id: str,
        *,
        outcome: str,
        reason: str | None = None,
        follow_up_task_id: str | None = None,
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _close_task_transition(
            self.root,
            task_id,
            outcome=outcome,
            reason=reason,
            follow_up_task_id=follow_up_task_id,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    def park(
        self,
        task_id: str,
        *,
        reason: str = "Task parked via CLI.",
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _park_task_transition(
            self.root,
            task_id,
            reason=reason,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    def abandon(
        self,
        task_id: str,
        *,
        reason: str = "Task abandoned via CLI.",
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _abandon_task_transition(
            self.root,
            task_id,
            reason=reason,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    def requeue(
        self,
        task_id: str,
        *,
        front: bool = False,
        force: bool = False,
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _requeue_task_transition(
            self.root,
            task_id,
            front=front,
            force=force,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    def resume(self, task_id: str, *, front: bool = False) -> TaskRecord:
        return _resume_task_transition(self.root, task_id, front=front)

    def update(
        self,
        task_id: str,
        *,
        title: str | object = ...,
        depends_on: list[str] | object = ...,
        model: str | None | object = ...,
        retry_limit: int | None | object = ...,
        priority: str | object = ...,
        goal: str | object = ...,
        acceptance_criteria: list[str] | object = ...,
        constraints: list[str] | object = ...,
        plan: list[str] | object = ...,
        auto_commit: bool | object = ...,
        outcome: str | None | object = ...,
        outcome_reason: str | None | object = ...,
        action: str | None | object = ...,
        allow_active_agent_task_mutation: bool = False,
        journal_message: str | None = None,
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _update_task_transition(
            self.root,
            task_id,
            title=title,
            depends_on=depends_on,
            model=model,
            retry_limit=retry_limit,
            priority=priority,
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            plan=plan,
            auto_commit=auto_commit,
            outcome=outcome,
            outcome_reason=outcome_reason,
            action=action,
            allow_active_agent_task_mutation=allow_active_agent_task_mutation,
            journal_message=journal_message,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )


def _reset_pipeline_state(root: Path, task_id: str, *, preserve_run_memory: bool = False) -> None:

    SqlitePersistence(root).reset_current_lifecycle_state(task_id, preserve_run_memory=preserve_run_memory)


def _terminate_subagent_pid(
    task_id: str,
    pid: int | None,
    *,
    wait_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
) -> bool:

    def _pid_is_dead() -> bool:
        if pid is None:
            return True
        try:
            reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            reaped_pid = 0
        if reaped_pid == pid:
            return True
        proc_status = Path(f"/proc/{pid}/status")
        if proc_status.exists():
            try:
                for line in proc_status.read_text(encoding="utf-8").splitlines():
                    if line.startswith("State:"):
                        return "\tZ" in line or " zombie" in line
            except OSError:
                pass
        return not runner_pid_is_alive(pid)

    if pid is None or _pid_is_dead():
        return False

    sleep_interval = max(poll_interval_seconds, 0.01)

    def _wait_until_dead(timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while not _pid_is_dead() and time.monotonic() < deadline:
            time.sleep(sleep_interval)
        return _pid_is_dead()

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False

    if _wait_until_dead(wait_timeout_seconds):
        return True

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True

    if _wait_until_dead(wait_timeout_seconds):
        return True

    raise WorkspaceConflictError(f"subagent pid {pid} for task {task_id} did not exit after SIGTERM/SIGKILL")


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
        if task.pipeline_status == "done":
            raise ValueError(f"Task {task.id} is already done")
        stage = task.runtime.pipeline.current_stage.stage or task.pipeline_status

        # Park the task - this is intentional operator action, not system interruption
        from litehive.domain.common import utcnow  # noqa: PLC0415
        from litehive.domain.runtime import RuntimeInterruptionState  # noqa: PLC0415

        now = utcnow()
        task.status = "parked"
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
            task.status = "queued"

        # Remove from active/queue state
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]

        # Re-add to queue front if remaining queued
        if task.status == "queued" and task.pipeline_status != "done":
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
                    _terminate_subagent_pid(
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


def _effective_task_engine(root: Path, task: TaskRecord) -> str:
    if task.runtime.execution.active_subagent is not None:
        return task.runtime.execution.active_subagent.engine
    if task.subagents:
        return task.subagents[-1].engine
    return load_config(root).default_engine


def _switch_prior_work_paths(root: Path, task: TaskRecord) -> list[str]:
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
    *,
    reason: str,
    previous_engine: str,
    new_engine: str,
    prior_work_paths: list[str],
) -> str:
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
    *,
    engine: str,
    reason: str,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> SwitchTaskSummary:

    if engine not in VALID_TASK_ENGINES:
        raise ValueError(f"Unsupported engine '{engine}'")
    if not reason.strip():
        raise ValueError("Switch reason must not be empty")

    task = require_task(root, task_id)
    before_task = snapshot_task_audit_state(task)
    if task.pipeline_status == "done":
        raise ValueError(f"Task {task.id} is already done")
    if task.pipeline_status == "backlog":
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

    if task.status == "queued":
        move_queued_task(root, task.id, 1)
        task = require_task(root, task.id)
    elif task.status in {"interrupted", "parked", "flagged", *CLOSED_TASK_STATUSES}:
        task = resume_task(root, task.id, front=True)
    else:
        raise ValueError(f"Task {task.id} is {task.status} and cannot be switched into a queued runnable state")

    prior_work_paths = _switch_prior_work_paths(root, task)

    append_task_activity(
        root,
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
        root,
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


def _persist_transition(
    root: Path,
    *,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str,
    action: str,
    actor: str,
    source: str,
    before_task: TaskRecord | TaskAuditState | None,
    before_queue: list[str],
    context: dict[str, object] | None = None,
) -> None:

    persist_task_and_state_without_runner_guard(
        root,
        task=task,
        state=state,
        journal_message=journal_message,
        audit_entries=[
            build_task_audit_entry(
                task_id=task.id,
                action=action,
                actor=actor,
                source=source,
                before_task=before_task,
                after_task=task,
                before_queue=before_queue,
                after_queue=state.queue,
                context=context,
            )
        ],
    )


def _requeue_task_transition(
    root: Path,
    task_id: str,
    *,
    front: bool = False,
    force: bool = False,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:

    def _task_checkout_path(task: TaskRecord) -> Path:
        worktree_path = resolve_recorded_worktree_path(
            root, task.runtime.pipeline.git.worktree_path or task.git.worktree_path
        )
        if worktree_path is not None and worktree_path.exists():
            return worktree_path
        return root

    def _path_differs_from_main(checkout_path: Path, main_ref: str, relative_path: str) -> bool:
        try:
            return path_differs_at_ref(checkout_path, main_ref, relative_path)
        except GitError as exc:
            raise ValueError(str(exc)) from exc

    with workspace_lock(root):
        task = get_task_record(root, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        flag_count_before = task.flag_count
        if task.flag_count >= 3 and not force:
            raise ValueError(f"Task {task.id} has been flagged {task.flag_count} times. Use --force to requeue anyway.")
        blocked_failed_runs = blocking_failed_run_records(task)
        failed_run_overrides: list[dict[str, object]] = []
        if blocked_failed_runs and not force:
            raise ValueError(failed_run_block_message(task, blocked_failed_runs))
        if blocked_failed_runs and force:
            failed_run_overrides = mark_failed_run_operator_override(root, task, blocked_failed_runs)
        state = load_state(root)
        queue_before = list(state.queue)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {"flagged", "parked", *CLOSED_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not flagged, parked, or closed")
        main_ref = current_head(root)
        if main_ref is not None:
            checkout_path = _task_checkout_path(task)
            activity_entries = load_task_activity(root, task)
            changed = False
            for entry in activity_entries:
                if not is_retractable_pass_entry(entry):
                    continue
                claimed_paths = normalized_files_changed(entry.files_changed)
                if any(_path_differs_from_main(checkout_path, main_ref, path) for path in claimed_paths):
                    continue
                changed = retract_activity_entry(entry) or changed
            if changed:
                save_task_activity(root, task, activity_entries)
        reset_task_for_recovery(
            task,
            status="queued",
            pipeline_status=implementation_entry_stage(task),
            clear_last_outcome=task.status not in {"flagged", "parked"},
        )
        _reset_pipeline_state(root, task.id, preserve_run_memory=True)
        _queue_task(state, task.id, front=front)
        _persist_transition(
            root,
            task=task,
            state=state,
            journal_message="Task requeued for another implementation pass.",
            action="requeued",
            actor=audit_actor,
            source=audit_source,
            before_task=before_task,
            before_queue=queue_before,
            context={
                "front": front,
                "force": force,
                "flag_count_before": flag_count_before,
                "stage_retry_exhaustion_overrides": failed_run_overrides,
            },
        )
        return task


def _resume_task_transition(root: Path, task_id: str, *, front: bool = False) -> TaskRecord:

    with workspace_lock(root):
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        state = load_state(root)
        queue_before = list(state.queue)
        resumed_stage = resumable_queue_stage(task)
        stranded_in_progress = (
            task.status == "in_progress"
            and task.runtime.pipeline.execution_status in {"interrupted", "idle"}
            and resumed_stage is not None
        )
        already_queued_resumable = task.status == "queued" and resumed_stage is not None
        if state.active_task_id == task.id and task.runtime.pipeline.execution_status != "running":
            state.active_task_id = None
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if (
            task.status not in {"flagged", *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}
            and not stranded_in_progress
            and not already_queued_resumable
        ):
            raise ValueError(f"Task {task.id} is not interrupted, parked, flagged, or closed")
        if resumed_stage is None:
            raise ValueError(f"Task {task.id} has no resumable stage")
        if resumed_stage in {TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING}:
            original_pipeline_status = task.pipeline_status
            task.pipeline_status = resumed_stage
            resumed_stage = reroute_stage_for_acceptance_criteria(task)
            task.pipeline_status = original_pipeline_status
        reset_task_for_recovery(
            task,
            status="queued",
            pipeline_status=resumed_stage,
            clear_last_outcome=task.status not in {"interrupted", "parked", "flagged"}
            and not stranded_in_progress
            and not already_queued_resumable,
        )
        _reset_pipeline_state(root, task.id)
        _queue_task(state, task.id, front=front)
        _persist_transition(
            root,
            task=task,
            state=state,
            journal_message=f"Task resumed from `{resumed_stage}`.",
            action="resumed",
            actor="operator",
            source="cli",
            before_task=before_task,
            before_queue=queue_before,
            context={
                "front": front,
                "resumed_stage": resumed_stage,
                "stranded_in_progress": stranded_in_progress,
            },
        )
        return task


def _abandon_task_transition(
    root: Path,
    task_id: str,
    *,
    reason: str = "Task abandoned via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:

    with workspace_lock(root):
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        state = load_state(root)
        queue_before = list(state.queue)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {"flagged", *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not interrupted, parked, flagged, or closed")
        _terminate_subagent_pid(
            task.id,
            None if task.runtime.execution.active_subagent is None else task.runtime.execution.active_subagent.pid,
        )
        _apply_cancelled_task_state(task, reason=reason)
        drop_task_from_workspace_state(state, task.id)
        _persist_transition(
            root,
            task=task,
            state=state,
            journal_message=f"{reason.rstrip('.')} at stage `{task.pipeline_status}`.",
            action="abandoned",
            actor=audit_actor,
            source=audit_source,
            before_task=before_task,
            before_queue=queue_before,
            context={"reason": reason},
        )
        _reset_pipeline_state(root, task.id)
        return task


_CLOSE_OUTCOME_REASON_CODES = {"done", "wont_do", "deferred", "duplicate", "execution_cancelled"}

_CLOSE_REASON_CODE_LABELS: dict[str, str] = {
    "done": "Task already satisfied.",
    "wont_do": "Task closed as won't do.",
    "deferred": "Task deferred.",
    "duplicate": "Task closed as duplicate.",
    "execution_cancelled": "Task abandoned via CLI.",
}


def _queue_task(state: WorkspaceState, task_id: str, *, front: bool = False) -> None:
    state.queue = [item for item in state.queue if item != task_id]
    if front:
        state.queue.insert(0, task_id)
    else:
        state.queue.append(task_id)


def _apply_cancelled_task_state(task: TaskRecord, *, reason: str) -> None:
    clear_task_run_activity(task, execution_status="cancelled")
    task.status = "closed"
    task.close_reason = "execution_cancelled"
    task.flag_reason = None
    apply_task_outcome(
        task,
        kind="closed",
        stage=task.pipeline_status,
        reason_code="execution_cancelled",
        reason=reason,
        retry_count=0,
        retry_limit=0,
    )


def _apply_close_task_state(
    task: TaskRecord,
    *,
    outcome: str,
    reason: str | None,
    follow_up_task_id: str | None = None,
    pipeline_status: str | None = None,
) -> str:
    execution_status = "done" if outcome == "done" else "cancelled"
    clear_task_run_activity(task, execution_status=execution_status)
    task.status = "done" if outcome == "done" else "closed"
    task.close_reason = outcome
    task.flag_reason = None
    task.pipeline_status = pipeline_status or ("done" if outcome == "done" else task.pipeline_status)
    apply_task_outcome(
        task,
        kind=task.status,
        stage=task.pipeline_status,
        reason_code=outcome,
        reason=reason or _CLOSE_REASON_CODE_LABELS[outcome],
        retry_count=0,
        retry_limit=0,
        follow_up_task_id=follow_up_task_id,
    )
    journal_message = f"Task closed: {outcome}."
    if reason:
        journal_message += f" {reason}"
    if follow_up_task_id is not None:
        journal_message += f" Follow-up task: {follow_up_task_id}."
    return journal_message


def _apply_parked_task_state(task: TaskRecord) -> None:
    clear_task_run_activity(task, execution_status="paused")
    task.status = "parked"


def _close_task_transition(
    root: Path,
    task_id: str,
    *,
    outcome: str,
    reason: str | None = None,
    follow_up_task_id: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:

    """Mark a task as explicitly closed with a terminal outcome.

    Valid outcomes: ``done``, ``wont_do``, ``deferred``, ``duplicate``, ``execution_cancelled``.
    The task is removed from the queue.
    """
    if outcome not in _CLOSE_OUTCOME_REASON_CODES:
        allowed = ", ".join(sorted(_CLOSE_OUTCOME_REASON_CODES))
        raise ValueError(f"Unsupported close outcome '{outcome}'. Expected one of: {allowed}")
    state = load_state(root)
    stop_summary: StopTaskSummary | None = None
    task_snapshot = get_task_record(root, task_id)
    active_subagent_pid = (
        None
        if task_snapshot is None or task_snapshot.runtime.execution.active_subagent is None
        else task_snapshot.runtime.execution.active_subagent.pid
    )
    runner_metadata = read_runner_lock_metadata(root) if runner_lock_is_held(root) else None
    if state.active_task_id == task_id or (runner_metadata is not None and runner_metadata.active_task_id == task_id):
        stop_summary = stop_current_task(root)
    runner_pid = None if stop_summary is None else stop_summary.runner_pid
    _terminate_subagent_pid(task_id, active_subagent_pid)
    _terminate_subagent_pid(task_id, runner_pid)
    with workspace_lock(root):
        task = get_task_record(root, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        if follow_up_task_id is not None:
            follow_up_task_id = follow_up_task_id.strip()
            if not follow_up_task_id:
                raise ValueError("Follow-up task id must not be empty")
            if follow_up_task_id == task.id:
                raise ValueError(f"Task {task.id} cannot reference itself as a follow-up task")
            if get_task_record(root, follow_up_task_id) is None:
                raise ValueError(f"Task {follow_up_task_id} not found")
        state = load_state(root)
        queue_before = list(state.queue)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == "done":
            raise ValueError(f"Task {task.id} is already done and cannot be closed")
        journal_message = _apply_close_task_state(
            task,
            outcome=outcome,
            reason=reason,
            follow_up_task_id=follow_up_task_id,
        )
        drop_task_from_workspace_state(state, task.id)
        _persist_transition(
            root,
            task=task,
            state=state,
            journal_message=journal_message,
            action="closed",
            actor=audit_actor,
            source=audit_source,
            before_task=before_task,
            before_queue=queue_before,
            context={
                "outcome": outcome,
                "reason": reason,
                "follow_up_task_id": follow_up_task_id,
            },
        )
        _reset_pipeline_state(root, task.id)
        return task


def _park_task_transition(
    root: Path,
    task_id: str,
    *,
    reason: str = "Task parked via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:

    """Mark a task as parked.

    The task is removed from the queue and set to status 'parked'.
    """
    with workspace_lock(root):
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        state = load_state(root)
        queue_before = list(state.queue)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == "done":
            raise ValueError(f"Task {task.id} is already done and cannot be parked")
        _apply_parked_task_state(task)
        drop_task_from_workspace_state(state, task.id)
        _persist_transition(
            root,
            task=task,
            state=state,
            journal_message=f"{reason.rstrip('.')} at stage `{task.pipeline_status}`.",
            action="parked",
            actor=audit_actor,
            source=audit_source,
            before_task=before_task,
            before_queue=queue_before,
            context={"reason": reason},
        )
        return task


def _update_task_transition(
    root: Path,
    task_id: str,
    *,
    title: str | object = ...,
    depends_on: list[str] | object = ...,
    model: str | None | object = ...,
    retry_limit: int | None | object = ...,
    priority: str | object = ...,
    goal: str | object = ...,
    acceptance_criteria: list[str] | object = ...,
    constraints: list[str] | object = ...,
    plan: list[str] | object = ...,
    auto_commit: bool | object = ...,
    outcome: str | None | object = ...,
    outcome_reason: str | None | object = ...,
    action: str | None | object = ...,
    allow_active_agent_task_mutation: bool = False,
    journal_message: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:

    if outcome is not ... and outcome is not None:
        return close_task(
            root,
            task_id,
            outcome=str(outcome),
            reason=str(outcome_reason) if outcome_reason is not ... and outcome_reason is not None else None,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    if action is not ... and action is not None:
        if action == "park":
            return park_task(
                root,
                task_id,
                reason="Task parked via structured report.",
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        if action == "requeue":
            return requeue_task(
                root,
                task_id,
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        if action == "abandon":
            return abandon_task(
                root,
                task_id,
                reason="Task abandoned via structured report.",
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        raise ValueError(f"Unsupported action '{action}'")

    with workspace_lock(root):
        state = load_state(root)
        task = get_task_record(root, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        queue_before = list(state.queue)
        # Skip the conflict guard when the current thread is the runner
        # (e.g., apply_task_updates_from_report during grooming).
        owner_thread_id = threading.get_ident()
        with RUNNER_LOCKS_MUTEX:
            runner_state = RUNNER_LOCKS.get(root.resolve())
        is_runner_thread = runner_state is not None and runner_state.owner_thread_id == owner_thread_id
        allow_active_task_mutation = allow_active_agent_task_mutation and state.active_task_id == task.id
        if not is_runner_thread and not allow_active_task_mutation:
            ensure_future_task_mutation_allowed(root, [task.id], state=state)

        if depends_on is not ...:
            validate_task_dependencies(root, task_id=task.id, depends_on=list(depends_on))
            task.depends_on = list(depends_on)

        if title is not ...:
            task.title = str(title)


        if model is not ...:
            task.model = model

        if retry_limit is not ...:
            if retry_limit is not None and retry_limit < 0:
                raise ValueError("Retry limit must be 0 or greater")
            task.retry_policy.max_retries = retry_limit

        if priority is not ...:
            if priority not in VALID_TASK_PRIORITIES:
                raise ValueError(f"Unsupported priority '{priority}'")
            task.priority = priority

        if goal is not ...:
            task.goal = goal

        if acceptance_criteria is not ...:
            task.acceptance_criteria = normalize_acceptance_criteria(list(acceptance_criteria))

        if constraints is not ...:
            task.constraints = normalize_task_text_list(list(constraints))

        if plan is not ...:
            task.plan = normalize_task_text_list(list(plan))

        if auto_commit is not ...:
            task.git.auto_commit = auto_commit

        task.pipeline_status = reroute_stage_for_acceptance_criteria(task)
        changed_fields = [
            name
            for name, changed in (
                ("depends_on", depends_on is not ...),
                ("title", title is not ...),
                ("model", model is not ...),
                ("retry_limit", retry_limit is not ...),
                ("priority", priority is not ...),
                ("goal", goal is not ...),
                ("acceptance_criteria", acceptance_criteria is not ...),
                ("constraints", constraints is not ...),
                ("plan", plan is not ...),
                ("auto_commit", auto_commit is not ...),
            )
            if changed
        ]

        if journal_message is None:
            journal_message = "Task metadata updated via CLI."
        if task.pipeline_status == PipelineStatus.GROOMING and missing_acceptance_criteria_reason(task) is not None:
            journal_message += " Rerouted to `grooming` until structured acceptance criteria are added."
        persist_future_task_update(
            root,
            task,
            journal_message=journal_message,
            audit_entries=[
                build_task_audit_entry(
                    task_id=task.id,
                    action="metadata_updated",
                    actor=audit_actor,
                    source=audit_source,
                    before_task=before_task,
                    after_task=task,
                    before_queue=queue_before,
                    after_queue=state.queue,
                    context={"changed_fields": changed_fields},
                )
            ],
        )
        return task


def task_transition_service(root: Path) -> TaskTransitionService:
    return TaskTransitionService(root)


def requeue_task(
    root: Path,
    task_id: str,
    *,
    front: bool = False,
    force: bool = False,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    return task_transition_service(root).requeue(
        task_id,
        front=front,
        force=force,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def resume_task(root: Path, task_id: str, *, front: bool = False) -> TaskRecord:
    return task_transition_service(root).resume(task_id, front=front)


def abandon_task(
    root: Path,
    task_id: str,
    *,
    reason: str = "Task abandoned via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    return task_transition_service(root).abandon(
        task_id,
        reason=reason,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def close_task(
    root: Path,
    task_id: str,
    *,
    outcome: str,
    reason: str | None = None,
    follow_up_task_id: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    return task_transition_service(root).close(
        task_id,
        outcome=outcome,
        reason=reason,
        follow_up_task_id=follow_up_task_id,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def park_task(
    root: Path,
    task_id: str,
    *,
    reason: str = "Task parked via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    return task_transition_service(root).park(
        task_id,
        reason=reason,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def update_task(
    root: Path,
    task_id: str,
    *,
    title: str | object = ...,
    depends_on: list[str] | object = ...,
    model: str | None | object = ...,
    retry_limit: int | None | object = ...,
    priority: str | object = ...,
    goal: str | object = ...,
    acceptance_criteria: list[str] | object = ...,
    constraints: list[str] | object = ...,
    plan: list[str] | object = ...,
    auto_commit: bool | object = ...,
    outcome: str | None | object = ...,
    outcome_reason: str | None | object = ...,
    action: str | None | object = ...,
    allow_active_agent_task_mutation: bool = False,
    journal_message: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    return task_transition_service(root).update(
        task_id,
        title=title,
        depends_on=depends_on,
        model=model,
        retry_limit=retry_limit,
        priority=priority,
        goal=goal,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        plan=plan,
        auto_commit=auto_commit,
        outcome=outcome,
        outcome_reason=outcome_reason,
        action=action,
        allow_active_agent_task_mutation=allow_active_agent_task_mutation,
        journal_message=journal_message,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )
