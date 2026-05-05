"""Task status transitions: requeue, resume, abandon, close, park, update, stop, switch."""

import threading
from pathlib import Path

from litehive.git.ops import GitError, current_head, path_differs_at_ref
from litehive.domain.common import PipelineStatus, TaskStage, TaskStatus
from litehive.domain.task import TaskRecord, WorkspaceState

from litehive.tasks.constants import (
    CLOSED_TASK_STATUSES,
    RESUMABLE_TASK_STATUSES,
    VALID_TASK_PRIORITIES,
    RUNNER_LOCKS,
    RUNNER_LOCKS_MUTEX,
)
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.state.locking import (
    ensure_future_task_mutation_allowed,
    persist_future_task_update,
    read_runner_lock_metadata,
    runner_lock_is_held,
    workspace_lock,
)
from litehive.state.persist import load_state, persist_task_and_state_without_runner_guard
from litehive.state.records import get_task_record, require_task
from litehive.tasks.activity import (
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
    build_task_audit_entry,
    snapshot_task_audit_state,
)
from litehive.tasks.failed_runs import (
    blocking_failed_run_records,
    failed_run_block_message,
    mark_failed_run_operator_override,
)
from litehive.domain.task_ops import StopTaskSummary
from litehive.tasks.normalization import (
    missing_acceptance_criteria_reason,
    normalize_acceptance_criteria,
    normalize_task_text_list,
    reroute_stage_for_acceptance_criteria,
    implementation_entry_stage,
)
from litehive.tasks.queue import (
    drop_task_from_workspace_state,
    reset_task_for_recovery,
    resumable_queue_stage,
    validate_task_dependencies,
)
from litehive.tasks._process_signals import terminate_subagent_pid
from litehive.tasks.runtime import apply_task_outcome, clear_task_run_activity
from litehive.worktree import resolve_recorded_worktree_path


# Per docs/feedback-2026-05-03.md (R10c): no class shells whose
# methods only delegate to free functions. The lifecycle transitions
# below are the actual implementations; the public API is the
# ``requeue_task`` / ``close_task`` / ``park_task`` / ``abandon_task``
# / ``resume_task`` / ``update_task`` wrappers at the bottom of this
# module.


def _reset_pipeline_state(root: Path, task_id: str, preserve_run_memory: bool = False) -> None:
    """Wipe the SQLite-side lifecycle/runtime rows for a task before it starts a new attempt; preserve_run_memory keeps recovery evidence so a requeue still has the prior failure context to feed back."""
    SqlitePersistence(root).reset_current_lifecycle_state(task_id, preserve_run_memory=preserve_run_memory)


# Process-signaling helper extracted to ``tasks/_process_signals.py``;
# re-aliased here so the existing ``_terminate_subagent_pid(...)`` call
# sites in this module keep working without churn.
_terminate_subagent_pid = terminate_subagent_pid


# Stop flow extracted to ``tasks/stop.py``. Re-imported here so the
# existing transition helpers (close/abandon/update) can call
# ``stop_current_task(root)`` without churn, and so external callers
# of ``litehive.tasks.status.stop_current_task`` keep working.
from litehive.tasks.stop import stop_current_task  # noqa: F401, E402


# Engine switching extracted to ``tasks/switch_engine.py`` so this
# module no longer carries the operator-flow plumbing.
from litehive.tasks.switch_engine import switch_task_engine  # noqa: F401, E402


def _persist_transition(
    root: Path,
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
    """Bundle a task/queue mutation with its audit entry so every status transition emits one consistent journal+audit row; shared by every transition in this module to keep the audit shape uniform."""

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
    front: bool = False,
    force: bool = False,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """Reset a flagged/parked/closed task back to the implementation entry stage and put it on the queue; retracts already-merged pass entries so the next implementation attempt does not double-claim work that is already on main."""

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
        if task.status not in {TaskStatus.FLAGGED, TaskStatus.PARKED, *CLOSED_TASK_STATUSES}:
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
            clear_last_outcome=task.status not in {TaskStatus.FLAGGED, TaskStatus.PARKED},
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


def _resume_task_transition(root: Path, task_id: str, front: bool = False) -> TaskRecord:
    """Pick the right pipeline stage to re-enter for an interrupted/parked/flagged task and queue it from there; preserves the prior outcome only when the operator is genuinely resuming, not when they want a clean retry."""

    with workspace_lock(root):
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        state = load_state(root)
        queue_before = list(state.queue)
        resumed_stage = resumable_queue_stage(task)
        stranded_in_progress = (
            task.status == TaskStatus.IN_PROGRESS
            and task.runtime.pipeline.execution_status in {"interrupted", "idle"}
            and resumed_stage is not None
        )
        already_queued_resumable = task.status == TaskStatus.QUEUED and resumed_stage is not None
        if state.active_task_id == task.id and task.runtime.pipeline.execution_status != "running":
            state.active_task_id = None
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if (
            task.status not in {TaskStatus.FLAGGED, *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}
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
            clear_last_outcome=task.status not in {TaskStatus.INTERRUPTED, TaskStatus.PARKED, TaskStatus.FLAGGED}
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
    reason: str = "Task abandoned via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """Cancel an in-flight or parked task: signal the live subagent if any, mark the task closed/cancelled, and drop it from the queue; differs from close_task in that abandon is the operator-initiated kill path while close records a deliberate terminal outcome."""

    with workspace_lock(root):
        task = require_task(root, task_id)
        before_task = snapshot_task_audit_state(task)
        state = load_state(root)
        queue_before = list(state.queue)
        ensure_future_task_mutation_allowed(root, [task.id], state=state)
        if task.status not in {TaskStatus.FLAGGED, *CLOSED_TASK_STATUSES, *RESUMABLE_TASK_STATUSES}:
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


def _queue_task(state: WorkspaceState, task_id: str, front: bool = False) -> None:
    """Place a task into the workspace queue without ever creating a duplicate entry; called by requeue and resume so a task that was already queued ends up at exactly one position."""
    state.queue = [item for item in state.queue if item != task_id]
    if front:
        state.queue.insert(0, task_id)
    else:
        state.queue.append(task_id)


def _apply_cancelled_task_state(task: TaskRecord, reason: str) -> None:
    clear_task_run_activity(task, execution_status="cancelled")
    task.status = TaskStatus.CLOSED
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
    outcome: str,
    reason: str | None,
    follow_up_task_id: str | None = None,
    pipeline_status: str | None = None,
) -> str:
    if outcome == "done":
        execution_status = "done"
    else:
        execution_status = "cancelled"
    clear_task_run_activity(task, execution_status=execution_status)
    if outcome == "done":
        task.status = TaskStatus.DONE
    else:
        task.status = TaskStatus.CLOSED
    task.close_reason = outcome
    task.flag_reason = None
    task.pipeline_status = pipeline_status or (PipelineStatus.DONE if outcome == "done" else task.pipeline_status)
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
    task.status = TaskStatus.PARKED


def _close_task_transition(
    root: Path,
    task_id: str,
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
    if task_snapshot is None or task_snapshot.runtime.execution.active_subagent is None:
        active_subagent_pid = None
    else:
        active_subagent_pid = task_snapshot.runtime.execution.active_subagent.pid
    if runner_lock_is_held(root):
        runner_metadata = read_runner_lock_metadata(root)
    else:
        runner_metadata = None
    if state.active_task_id == task_id or (runner_metadata is not None and runner_metadata.active_task_id == task_id):
        stop_summary = stop_current_task(root)
    if stop_summary is None:
        runner_pid = None
    else:
        runner_pid = stop_summary.runner_pid
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
        if task.status == TaskStatus.DONE:
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
        if task.status == TaskStatus.DONE:
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
    """Edit task metadata or route the operator's intent into a terminal transition (close/park/requeue/abandon); uses ``...`` sentinels so callers can distinguish "leave field alone" from "set to None"."""

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


def requeue_task(
    root: Path,
    task_id: str,
    front: bool = False,
    force: bool = False,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """Public CLI/agent entry for sending a flagged, parked, or closed task back to the implementation queue for another pass."""
    return _requeue_task_transition(
        root,
        task_id,
        front=front,
        force=force,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def resume_task(root: Path, task_id: str, front: bool = False) -> TaskRecord:
    """Public CLI/agent entry for putting an interrupted, parked, or stranded task back on the queue at the stage it was last working on."""
    return _resume_task_transition(root, task_id, front=front)


def abandon_task(
    root: Path,
    task_id: str,
    reason: str = "Task abandoned via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """Public CLI/agent entry for the operator-initiated kill path that signals the live subagent and marks the task cancelled."""
    return _abandon_task_transition(
        root,
        task_id,
        reason=reason,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def close_task(
    root: Path,
    task_id: str,
    outcome: str,
    reason: str | None = None,
    follow_up_task_id: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """Public CLI/agent entry for terminating a task with a deliberate verdict (done, wont_do, deferred, duplicate, execution_cancelled) and optional follow-up reference."""
    return _close_task_transition(
        root,
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
    reason: str = "Task parked via CLI.",
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """Public CLI/agent entry for taking a task out of the queue without closing it, so the operator can pick it up again later via resume."""
    return _park_task_transition(
        root,
        task_id,
        reason=reason,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )


def update_task(
    root: Path,
    task_id: str,
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
    """Public CLI/agent entry that either edits task metadata or routes the operator's intent into the matching terminal transition (close/park/requeue/abandon)."""
    return _update_task_transition(
        root,
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
