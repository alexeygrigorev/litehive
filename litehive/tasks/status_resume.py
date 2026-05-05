"""Task status transitions for re-entering a task after a stop or failure.

Covers ``requeue_task`` (start over from the implementation entry stage)
and ``resume_task`` (continue from the stage that was last in flight).
Both push a task back onto the workspace queue; they differ in what
prior progress they preserve and how they pick the re-entry stage.
"""

from pathlib import Path

from litehive.git.ops import GitError, current_head, path_differs_at_ref
from litehive.domain.common import TaskStage, TaskStatus
from litehive.domain.task import TaskRecord

from litehive.tasks.constants import (
    CLOSED_TASK_STATUSES,
    RESUMABLE_TASK_STATUSES,
)
from litehive.state.locking import (
    ensure_future_task_mutation_allowed,
    workspace_lock,
)
from litehive.state.persist import load_state
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
from litehive.tasks.audit import snapshot_task_audit_state
from litehive.tasks.failed_runs import (
    blocking_failed_run_records,
    failed_run_block_message,
    mark_failed_run_operator_override,
)
from litehive.tasks.normalization import (
    reroute_stage_for_acceptance_criteria,
    implementation_entry_stage,
)
from litehive.tasks.queue import (
    reset_task_for_recovery,
    resumable_queue_stage,
)
from litehive.tasks._status_helpers import (
    _persist_transition,
    _queue_task,
    _reset_pipeline_state,
)
from litehive.worktree import resolve_recorded_worktree_path


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
