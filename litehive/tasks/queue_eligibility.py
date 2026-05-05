"""Pure eligibility predicates and stage helpers used by the queue selector.

No I/O, no state mutation: these functions only inspect ``TaskRecord`` /
``WorkspaceState`` snapshots and return booleans, stage names, or dependency
information. They are the leaf module of the queue split — both
``queue_mutations`` and ``queue_selection`` import from here.
"""

from pathlib import Path

from litehive.domain.common import PipelineStatus, TaskStage, TaskStatus
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.state.records import list_tasks
from litehive.tasks.failed_runs import has_blocking_failed_run_history
from litehive.tasks.normalization import implementation_entry_stage

_TERMINAL_EXECUTION_STATUSES = {"done", "cancelled", "failed", "blocked", "interrupted"}
_TERMINAL_OUTCOME_KINDS = {"closed", "duplicate", "deferred", "wont_do"}
_TRUSTED_STAGE_MARKER_STATUSES = {"idle", "paused", "interrupted", "running"}
_RESUMABLE_PIPELINE_STAGES: frozenset[TaskStage] = frozenset(
    {TaskStage.GROOMING, TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING, TaskStage.COMMIT_TO_GIT}
)


def _normalize_resumable_stage_name(stage: str | None) -> str | None:
    if stage in _RESUMABLE_PIPELINE_STAGES:
        return stage
    return None


def resumable_queue_stage(task: TaskRecord) -> str | None:
    """Pick the pipeline stage a queued/idle task should resume at, or ``None`` if it must restart.

    Used by execution recovery and the ``resume`` lifecycle command to decide
    whether an interrupted task can pick up mid-pipeline (grooming through
    commit) instead of going back to ``backlog``.
    """
    interruption = task.runtime.execution.interruption
    current_stage = task.runtime.pipeline.current_stage
    candidates = [
        task.pipeline_status,
        None if interruption is None else interruption.resume_stage,
        None if interruption is None else interruption.pipeline_status,
    ]
    if current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        candidates.append(current_stage.stage)
    for candidate in candidates:
        normalized = _normalize_resumable_stage_name(None if candidate is None else str(candidate))
        if normalized is not None:
            return normalized
    return None


def resumable_running_stage(task: TaskRecord) -> str | None:
    """Pick the resume stage for a task whose runtime still claims to be ``running``.

    Stale-runner recovery calls this when it has just decided a "running" task
    is actually orphaned (subagent PID dead): it trusts the live current-stage
    marker first, then falls back to the queued-stage heuristics.
    """
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.status == "running":
        normalized = _normalize_resumable_stage_name(None if current_stage.stage is None else str(current_stage.stage))
        if normalized is not None:
            return normalized
    return resumable_queue_stage(task)


def _needs_manual_intervention(task: TaskRecord) -> bool:
    return has_blocking_failed_run_history(task) or (
        task.status == TaskStatus.FLAGGED
        and (
            task.flag_count >= 3
            or task.flag_reason
            in {
                "hook_reject_loop",
                "merge_failed",
                "rejection_loop_detected",
                "semantic_reject",
                "time_budget_exceeded",
            }
        )
    )


def _is_recovery_budget_exhausted(task: TaskRecord) -> bool:
    return task.status == TaskStatus.FLAGGED and task.flag_reason in {
        "crash_budget_exhausted",
        "recovery_budget_exhausted",
        "recovery_failed",
    }


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    return task.pipeline_status == PipelineStatus.COMMIT_TO_GIT and task.status in {
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.INTERRUPTED,
    }


def _has_terminal_execution_status(task: TaskRecord) -> bool:
    return str(task.runtime.pipeline.execution_status) in _TERMINAL_EXECUTION_STATUSES


def _has_terminal_outcome_kind(task: TaskRecord) -> bool:
    kind = task.runtime.pipeline.last_outcome.kind
    return kind is not None and str(kind) in _TERMINAL_OUTCOME_KINDS


def task_has_resume_marker(task: TaskRecord) -> bool:
    """Tell whether a task's runtime still vouches for its declared pipeline stage.

    The stale-pipeline normalizer (and the workspace-repair pass that mirrors
    it) calls this before demoting a queued task back to ``backlog`` — a task
    with a trustworthy stage marker or a matching interruption record must be
    left alone so it can resume in place.
    """
    stage = str(task.pipeline_status)
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.stage == stage and current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        return True
    interruption = task.runtime.execution.interruption
    if interruption is not None and (interruption.resume_stage == stage or interruption.pipeline_status == stage):
        return True
    return False


def _is_parked_task(task: TaskRecord) -> bool:
    return task.status == TaskStatus.PARKED


def is_task_eligible_for_execution(task: TaskRecord) -> bool:
    """Decide whether a task is allowed to be dequeued or kept active right now.

    The single source of truth for "is this task runnable?" — the queue
    selector, workspace-repair flows, status diagnostics, and the
    single-active-task invariant in the workspace lock all consult this so
    they share the same view of terminal/blocked statuses.
    """
    if has_blocking_failed_run_history(task):
        return False
    if _has_terminal_execution_status(task):
        return False
    if _has_terminal_outcome_kind(task):
        return False
    if task.pipeline_status == PipelineStatus.DONE:
        return False
    if _needs_manual_intervention(task):
        return False
    if _is_recovery_budget_exhausted(task):
        return False
    if task.status in {TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.FLAGGED}:
        return True
    if task.status == TaskStatus.INTERRUPTED:
        return True
    return False


def _auto_recovery_stage_for_flagged_task(task: TaskRecord) -> str:
    if task.pipeline_status == PipelineStatus.COMMIT_TO_GIT:
        return TaskStage.COMMIT_TO_GIT.value
    return implementation_entry_stage(task)


def _is_task_completed(task: TaskRecord) -> bool:
    return task.status == TaskStatus.DONE and task.pipeline_status == PipelineStatus.DONE


def _task_blockers(task: TaskRecord, tasks_by_id: dict[str, TaskRecord]) -> list[str]:
    blockers: list[str] = []
    seen: set[str] = set()
    for dependency_id in task.depends_on:
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        dependency = tasks_by_id.get(dependency_id)
        if dependency is None:
            blockers.append(f"{dependency_id} (missing)")
            continue
        if not _is_task_completed(dependency):
            blockers.append(f"{dependency.id} ({dependency.status}/{dependency.pipeline_status})")
    return blockers


def validate_task_dependencies(root: Path, task_id: str, depends_on: list[str]) -> None:
    """Reject a task whose ``depends_on`` list is self-referential, missing, or cyclic.

    Called when persisting a new task and when ``litehive update`` rewrites a
    task's dependency list — refuses the mutation before it can corrupt the
    queue selector's blocked-task graph.
    """
    tasks_by_id = {task.id: task for task in list_tasks(root, strict=False)}
    seen: set[str] = set()
    for dependency_id in depends_on:
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        if dependency_id == task_id:
            raise ValueError(f"Task {task_id} cannot depend on itself")
        if dependency_id not in tasks_by_id:
            raise ValueError(f"Task {dependency_id} not found")
        if _dependency_reaches_task(task_id, dependency_id, tasks_by_id):
            raise ValueError(f"Task {task_id} dependency cycle detected via {dependency_id}")


def _dependency_reaches_task(task_id: str, dependency_id: str, tasks_by_id: dict[str, TaskRecord]) -> bool:
    stack = [dependency_id]
    seen: set[str] = set()
    while stack:
        current_id = stack.pop()
        if current_id == task_id:
            return True
        if current_id in seen:
            continue
        seen.add(current_id)
        current = tasks_by_id.get(current_id)
        if current is None:
            continue
        stack.extend(current.depends_on)
    return False


def _is_interrupted_task(task: TaskRecord) -> bool:
    return is_task_eligible_for_execution(task) and (
        task.status == TaskStatus.IN_PROGRESS or task.pipeline_status != PipelineStatus.BACKLOG
    )


def _live_active_pipeline_stage(state: WorkspaceState, tasks_by_id: dict[str, TaskRecord]) -> str | None:
    if state.active_task_id is None:
        return None
    active_task = tasks_by_id.get(state.active_task_id)
    if active_task is None:
        return None
    current_stage = active_task.runtime.pipeline.current_stage
    if str(active_task.runtime.pipeline.execution_status) == "running" or current_stage.status == "running":
        return str(current_stage.stage or active_task.pipeline_status)
    return None
