"""Task status transitions: requeue, resume, abandon, close, park, update, stop, switch."""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from litehive.config.loading import load_config
from litehive.git.ops import current_head
from litehive.domain.common import utcnow
from litehive.domain.task import TaskRecord, WorkspaceState

from litehive.tasks.constants import (
    CLOSED_TASK_STATUSES,
    RESUMABLE_TASK_STATUSES,
    VALID_TASK_ENGINES,
    VALID_TASK_PRIORITIES,
    VALID_TASK_TYPES,
    RUNNER_LOCKS,
    RUNNER_LOCKS_MUTEX,
)
from litehive.state.locking import workspace_lock
from litehive.domain.task_ops import StopTaskSummary, SwitchTaskSummary, WorkspaceConflictError
from litehive.tasks.normalization import (
    missing_acceptance_criteria_reason,
    normalize_acceptance_criteria,
    normalize_task_text_list,
    reroute_stage_for_acceptance_criteria,
    implementation_entry_stage,
)
from litehive.tasks.paths import latest_subagent_base, task_dir


def _active_task_id_for_stop(root: Path, state: WorkspaceState) -> str:
    from litehive.tasks.queue import validate_single_active_task, active_task_markers

    markers = active_task_markers(root, state)
    if not markers:
        raise ValueError("No active task to stop")
    if len(markers) > 1:
        validate_single_active_task(root, state)
    return next(iter(sorted(markers)))


def _stop_active_task_without_runner_guard(root: Path, task_id: str) -> TaskRecord:
    from litehive.state.records import require_task
    from litehive.state.persist import load_state
    from litehive.recovery.workspace_repair import prepare_interrupted_task, interruption_journal_message
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    with workspace_lock(root):
        state = load_state(root)
        active_task_id = _active_task_id_for_stop(root, state)
        if active_task_id != task_id:
            raise WorkspaceConflictError(
                f"task {task_id} is no longer the active task in this workspace"
            )
        task = require_task(root, task_id)
        if task.pipeline_status == "done":
            raise ValueError(f"Task {task.id} is already done")
        stage = task.runtime.current_stage.step or task.pipeline_status
        prepare_interrupted_task(
            root,
            task,
            stage=stage,
            summary=f"Execution interrupted via `litehive stop`. Resume from `{stage}`.",
            reason="Task stopped via CLI",
        )
        task.status = "parked"
        if stage == "commit_to_git":
            task.status = "queued"
        state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        if task.status == "queued" and task.pipeline_status != "done":
            state.queue.insert(0, task.id)
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=interruption_journal_message(task),
        )
        return task


def stop_current_task(
    root: Path,
    *,
    wait_timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
) -> StopTaskSummary:
    from litehive.state.records import require_task
    from litehive.state.persist import load_state
    from litehive.tasks.queue import active_task_markers
    from litehive.state.locking import (
        read_runner_lock_metadata,
        runner_lock_is_held,
        runner_pid_is_alive,
    )
    from litehive.recovery.workspace_repair import recover_stale_runner_state

    state = load_state(root)
    active_task_id = _active_task_id_for_stop(root, state)
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
    if task.runtime.active_subagent is not None:
        return task.runtime.active_subagent.engine
    if task.runtime.last_subagent is not None:
        return task.runtime.last_subagent.engine
    return load_config(root).default_engine


def _switch_prior_work_paths(root: Path, task: TaskRecord) -> list[str]:
    paths: list[str] = []
    handoff = task.runtime.continuation_handoff
    for candidate in (
        None if handoff is None else handoff.subagent_path,
        None if handoff is None else handoff.transcript_path,
        None if handoff is None else handoff.report_path,
        None if handoff is None else handoff.session_path,
        None if task.runtime.last_subagent is None else task.runtime.last_subagent.path,
    ):
        if candidate and candidate not in paths:
            paths.append(candidate)
    base = latest_subagent_base(root, task)
    if base is not None:
        rel_path = str(base.relative_to(task_dir(root, task)))
        if rel_path not in paths:
            paths.append(rel_path)
    return paths


def _switch_thread_comment_message(
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


def switch_task_engine(root: Path, task_id: str, *, engine: str, reason: str) -> SwitchTaskSummary:
    from litehive.state.records import require_task
    from litehive.state.persist import load_state
    from litehive.tasks.queue import move_queued_task
    from litehive.tasks.reports import append_thread_comment
    from litehive.tasks.runtime import mark_engine_switch

    if engine not in VALID_TASK_ENGINES:
        raise ValueError(f"Unsupported engine '{engine}'")
    if not reason.strip():
        raise ValueError("Switch reason must not be empty")

    task = require_task(root, task_id)
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
        task = require_task(root, task_id)

    previous_engine = _effective_task_engine(root, task)
    mark_engine_switch(
        root,
        task,
        step=task.pipeline_status,
        from_engine=previous_engine,
        to_engine=engine,
        reason=reason.strip(),
    )
    task = require_task(root, task.id)

    if task.status == "queued":
        move_queued_task(root, task.id, 1)
        task = require_task(root, task.id)
    elif task.status in {"interrupted", "parked", "flagged", "merge_failed", *CLOSED_TASK_STATUSES}:
        task = resume_task(root, task.id, front=True)
    else:
        raise ValueError(
            f"Task {task.id} is {task.status} and cannot be switched into a queued runnable state"
        )

    prior_work_paths = _switch_prior_work_paths(root, task)
    from litehive.domain.reports import TaskThreadComment

    append_thread_comment(
        root,
        task,
        TaskThreadComment(
            role="operator",
            step=task.pipeline_status,
            verdict="comment",
            message=_switch_thread_comment_message(
                task,
                reason=reason.strip(),
                previous_engine=previous_engine,
                new_engine=engine,
                prior_work_paths=prior_work_paths,
            ),
        ),
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



def requeue_task(root: Path, task_id: str, *, front: bool = False, force: bool = False) -> TaskRecord:
    from litehive.state.records import require_task
    from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
    from litehive.state.persist import load_state
    from litehive.tasks.queue import reset_task_for_recovery
    from litehive.tasks.reports import (
        normalized_files_changed,
        is_retractable_pass_comment,
        load_task_thread,
        retract_thread_comment,
        save_task_thread,
    )
    from litehive.tasks.worktrees import resolve_recorded_worktree_path
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    def _task_checkout_path(task: TaskRecord) -> Path:
        worktree_path = resolve_recorded_worktree_path(
            root, task.runtime.git.worktree_path or task.git.worktree_path
        )
        if worktree_path is not None and worktree_path.exists():
            return worktree_path
        return root

    def _path_differs_from_main(checkout_path: Path, main_ref: str, relative_path: str) -> bool:
        proc = subprocess.run(
            ["git", "diff", "--quiet", main_ref, "--", relative_path],
            cwd=checkout_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 1:
            return True
        if proc.returncode == 0:
            return False
        raise ValueError(proc.stderr.strip() or f"git diff failed for {relative_path}")

    with workspace_lock(root):
        task = require_task(root, task_id)
        if task.flag_count >= 3 and not force:
            raise ValueError(
                f"Task {task.id} has been flagged {task.flag_count} times. "
                "Use --force to requeue anyway."
            )
        state = load_state(root)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {"flagged", "merge_failed", "parked", *CLOSED_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not flagged, merge_failed, parked, or closed")
        main_ref = current_head(root)
        if main_ref is not None:
            checkout_path = _task_checkout_path(task)
            thread = load_task_thread(root, task)
            changed = False
            for comment in thread:
                if not is_retractable_pass_comment(comment):
                    continue
                claimed_paths = normalized_files_changed(comment.files_changed)
                if any(_path_differs_from_main(checkout_path, main_ref, path) for path in claimed_paths):
                    continue
                changed = retract_thread_comment(comment) or changed
            if changed:
                save_task_thread(root, task, thread)
        reset_task_for_recovery(
            task,
            status="queued",
            pipeline_status=implementation_entry_stage(task),
            clear_last_outcome=task.status not in {"flagged", "merge_failed", "parked"},
        )
        state.queue = [item for item in state.queue if item != task.id]
        if front:
            state.queue.insert(0, task.id)
        else:
            state.queue.append(task.id)
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message="Task requeued for another implementation pass.",
        )
        return task


def resume_task(root: Path, task_id: str, *, front: bool = False) -> TaskRecord:
    from litehive.state.records import require_task
    from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
    from litehive.state.persist import load_state
    from litehive.tasks.queue import reset_task_for_recovery
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    with workspace_lock(root):
        task = require_task(root, task_id)
        state = load_state(root)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {"flagged", "merge_failed", *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not interrupted, parked, flagged, merge_failed, or closed")
        if task.pipeline_status in {"backlog", "done"}:
            raise ValueError(f"Task {task.id} has no resumable stage")
        resumed_stage = task.pipeline_status
        if resumed_stage == "merge_failed":
            resumed_stage = "commit_to_git"
        if resumed_stage in {"implementing", "testing", "accepting"}:
            resumed_stage = reroute_stage_for_acceptance_criteria(task)
        reset_task_for_recovery(
            task,
            status="queued",
            pipeline_status=resumed_stage,
            clear_last_outcome=task.status not in {"interrupted", "parked", "flagged", "merge_failed"},
            preserve_continuation_handoff=task.status in {"interrupted", "parked"},
        )
        state.queue = [item for item in state.queue if item != task.id]
        if front:
            state.queue.insert(0, task.id)
        else:
            state.queue.append(task.id)
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=f"Task resumed from `{resumed_stage}`.",
        )
        return task


def abandon_task(root: Path, task_id: str) -> TaskRecord:
    from litehive.state.records import require_task
    from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
    from litehive.state.persist import load_state
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    with workspace_lock(root):
        task = require_task(root, task_id)
        state = load_state(root)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {"flagged", "merge_failed", *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
            raise ValueError(f"Task {task.id} is not interrupted, parked, flagged, merge_failed, or closed")
        task.status = "cancelled"
        task.runtime.execution_status = "cancelled"
        task.runtime.run_started_at = None
        task.runtime.updated_at = utcnow()
        task.runtime.active_subagent = None
        task.runtime.last_outcome.kind = "cancelled"
        task.runtime.last_outcome.stage = task.pipeline_status
        task.runtime.last_outcome.reason_code = "execution_cancelled"
        task.runtime.last_outcome.reason = "Task abandoned via CLI."
        task.runtime.last_outcome.retry_count = 0
        task.runtime.last_outcome.retry_limit = 0
        task.runtime.last_outcome.recorded_at = task.runtime.updated_at
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=f"Task abandoned via CLI at stage `{task.pipeline_status}`.",
        )
        return task


_CLOSE_OUTCOME_REASON_CODES = {"wont_do", "deferred", "duplicate", "execution_cancelled"}

_CLOSE_REASON_CODE_LABELS: dict[str, str] = {
    "wont_do": "Task closed as won't do.",
    "deferred": "Task deferred.",
    "duplicate": "Task closed as duplicate.",
    "execution_cancelled": "Task abandoned via CLI.",
}


def close_task(
    root: Path,
    task_id: str,
    *,
    outcome: str,
    reason: str | None = None,
    follow_up_task_id: str | None = None,
) -> TaskRecord:
    from litehive.state.records import require_task
    from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
    from litehive.state.persist import load_state
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    """Mark a task as explicitly closed with a non-implementation outcome.

    Valid outcomes: ``wont_do``, ``deferred``, ``duplicate``, ``execution_cancelled``.
    The task is removed from the queue.
    """
    if outcome not in _CLOSE_OUTCOME_REASON_CODES:
        allowed = ", ".join(sorted(_CLOSE_OUTCOME_REASON_CODES))
        raise ValueError(f"Unsupported close outcome '{outcome}'. Expected one of: {allowed}")
    state = load_state(root)
    if state.active_task_id == task_id:
        stop_current_task(root)
    with workspace_lock(root):
        task = require_task(root, task_id)
        if follow_up_task_id is not None:
            follow_up_task_id = follow_up_task_id.strip()
            if not follow_up_task_id:
                raise ValueError("Follow-up task id must not be empty")
            if follow_up_task_id == task.id:
                raise ValueError(f"Task {task.id} cannot reference itself as a follow-up task")
            require_task(root, follow_up_task_id)
        state = load_state(root)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == "done":
            raise ValueError(f"Task {task.id} is already done and cannot be closed")
        now = utcnow()
        task.status = outcome  # type: ignore[assignment]
        task.runtime.execution_status = "cancelled"
        task.runtime.run_started_at = None
        task.runtime.updated_at = now
        task.runtime.active_subagent = None
        task.runtime.last_outcome.kind = outcome  # type: ignore[assignment]
        task.runtime.last_outcome.stage = task.pipeline_status
        task.runtime.last_outcome.reason_code = outcome
        task.runtime.last_outcome.reason = reason or _CLOSE_REASON_CODE_LABELS[outcome]
        task.runtime.last_outcome.follow_up_task_id = follow_up_task_id
        task.runtime.last_outcome.retry_count = 0
        task.runtime.last_outcome.retry_limit = 0
        task.runtime.last_outcome.recorded_at = now
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        journal_message = f"Task closed: {outcome}."
        if reason:
            journal_message += f" {reason}"
        if follow_up_task_id is not None:
            journal_message += f" Follow-up task: {follow_up_task_id}."
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=journal_message,
        )
        return task


def park_task(root: Path, task_id: str) -> TaskRecord:
    from litehive.state.records import require_task
    from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
    from litehive.state.persist import load_state
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    """Mark a task as parked.

    The task is removed from the queue and set to status 'parked'.
    """
    with workspace_lock(root):
        task = require_task(root, task_id)
        state = load_state(root)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status == "done":
            raise ValueError(f"Task {task.id} is already done and cannot be parked")
        now = utcnow()
        task.status = "parked"
        task.runtime.execution_status = "paused"
        task.runtime.run_started_at = None
        task.runtime.updated_at = now
        task.runtime.active_subagent = None
        if state.active_task_id == task.id:
            state.active_task_id = None
        state.queue = [item for item in state.queue if item != task.id]
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=f"Task parked via CLI at stage `{task.pipeline_status}`.",
        )
        return task


def update_task(
    root: Path,
    task_id: str,
    *,
    title: str | object = ...,
    depends_on: list[str] | object = ...,
    task_type: str | None | object = ...,
    engine: str | None | object = ...,
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
    journal_message: str | None = None,
) -> TaskRecord:
    from litehive.state.records import require_task
    from litehive.state.locking import ensure_future_task_mutation_allowed, persist_future_task_update, workspace_lock
    from litehive.state.persist import load_state
    from litehive.tasks.queue import reset_task_for_recovery
    from litehive.tasks.queue import validate_task_dependencies
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    with workspace_lock(root):
        state = load_state(root)
        task = require_task(root, task_id)
        # Skip the conflict guard when the current thread is the runner
        # (e.g., apply_task_updates_from_report during grooming).
        owner_thread_id = threading.get_ident()
        with RUNNER_LOCKS_MUTEX:
            runner_state = RUNNER_LOCKS.get(root.resolve())
        is_runner_thread = runner_state is not None and runner_state.owner_thread_id == owner_thread_id
        if not is_runner_thread:
            ensure_future_task_mutation_allowed(root, [task.id], state=state)

        if outcome is not ... and outcome is not None:
            outcome_str = str(outcome)
            reason_str = (
                str(outcome_reason)
                if outcome_reason is not ... and outcome_reason is not None
                else None
            )
            if outcome_str not in _CLOSE_OUTCOME_REASON_CODES:
                allowed = ", ".join(sorted(_CLOSE_OUTCOME_REASON_CODES))
                raise ValueError(
                    f"Unsupported close outcome '{outcome_str}'. Expected one of: {allowed}"
                )
            if task.status == "done":
                raise ValueError(f"Task {task.id} is already done and cannot be closed")
            now = utcnow()
            task.status = outcome_str
            task.pipeline_status = "done"
            task.runtime.execution_status = "cancelled"
            task.runtime.run_started_at = None
            task.runtime.updated_at = now
            task.runtime.active_subagent = None
            task.runtime.last_outcome.kind = outcome_str
            task.runtime.last_outcome.stage = task.pipeline_status
            task.runtime.last_outcome.reason_code = outcome_str
            task.runtime.last_outcome.reason = reason_str or _CLOSE_REASON_CODE_LABELS.get(
                outcome_str, f"Task closed: {outcome_str}."
            )
            task.runtime.last_outcome.retry_count = 0
            task.runtime.last_outcome.retry_limit = 0
            task.runtime.last_outcome.recorded_at = now
            if state.active_task_id == task.id:
                state.active_task_id = None
            state.queue = [item for item in state.queue if item != task.id]
            close_msg = f"Task closed: {outcome_str}."
            if reason_str:
                close_msg += f" {reason_str}"
            persist_task_and_state_without_runner_guard(
                root,
                task=task,
                state=state,
                journal_message=close_msg,
            )
            return task

        if action is not ... and action is not None:
            if action == "park":
                if task.status == "done":
                    raise ValueError(f"Task {task.id} is already done and cannot be parked")
                now = utcnow()
                task.status = "parked"
                task.runtime.execution_status = "paused"
                task.runtime.run_started_at = None
                task.runtime.updated_at = now
                task.runtime.active_subagent = None
                if state.active_task_id == task.id:
                    state.active_task_id = None
                state.queue = [item for item in state.queue if item != task.id]
                persist_task_and_state_without_runner_guard(
                    root,
                    task=task,
                    state=state,
                    journal_message=f"Task parked via structured report at stage `{task.pipeline_status}`.",
                )
                return task
            if action == "requeue":
                if task.status not in {"flagged", "merge_failed", "parked", *CLOSED_TASK_STATUSES}:
                    raise ValueError(f"Task {task.id} is not flagged, merge_failed, parked, or closed")
                reset_task_for_recovery(
                    task,
                    status="queued",
                    pipeline_status=implementation_entry_stage(task),
                    clear_last_outcome=task.status not in {"flagged", "merge_failed", "parked"},
                )
                state.queue = [item for item in state.queue if item != task.id]
                state.queue.append(task.id)
                persist_task_and_state_without_runner_guard(
                    root,
                    task=task,
                    state=state,
                    journal_message="Task requeued for another implementation pass.",
                )
                return task
            if action == "abandon":
                if task.status not in {"flagged", "merge_failed", *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
                    raise ValueError(f"Task {task.id} is not interruptible or closed")
                now = utcnow()
                task.status = "cancelled"
                task.runtime.execution_status = "cancelled"
                task.runtime.run_started_at = None
                task.runtime.updated_at = now
                task.runtime.active_subagent = None
                task.runtime.last_outcome.kind = "cancelled"
                task.runtime.last_outcome.stage = task.pipeline_status
                task.runtime.last_outcome.reason_code = "execution_cancelled"
                task.runtime.last_outcome.reason = "Task abandoned via structured report."
                task.runtime.last_outcome.retry_count = 0
                task.runtime.last_outcome.retry_limit = 0
                task.runtime.last_outcome.recorded_at = now
                if state.active_task_id == task.id:
                    state.active_task_id = None
                state.queue = [item for item in state.queue if item != task.id]
                persist_task_and_state_without_runner_guard(
                    root,
                    task=task,
                    state=state,
                    journal_message=f"Task abandoned via structured report at stage `{task.pipeline_status}`.",
                )
                return task
            raise ValueError(f"Unsupported action '{action}'")

        if depends_on is not ...:
            validate_task_dependencies(root, task_id=task.id, depends_on=list(depends_on))
            task.depends_on = list(depends_on)

        if title is not ...:
            task.title = str(title)

        if task_type is not ...:
            if task_type is not None and task_type not in VALID_TASK_TYPES:
                raise ValueError(f"Unsupported task type '{task_type}'")
            task.task_type = task_type

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

        if journal_message is None:
            journal_message = "Task metadata updated via CLI."
        if (
            task.pipeline_status == "grooming"
            and missing_acceptance_criteria_reason(task) is not None
        ):
            journal_message += (
                " Rerouted to `grooming` until structured acceptance criteria are added."
            )
        persist_future_task_update(root, task, journal_message=journal_message)
        return task


update_task_metadata = update_task


update_task_metadata = update_task
