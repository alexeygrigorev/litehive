"""Pure eligibility predicates and stage helpers used by the queue selector.

No I/O, no state mutation: these functions only inspect ``TaskRecord`` /
``WorkspaceState`` snapshots and return booleans, stage names, or dependency
information. They are the leaf module of the queue split — both
``queue_mutations`` and ``queue_selection`` import from here.
"""

from litehive.domain.common import (
    PipelineStatus,
    RuntimeStageStatus,
    TaskExecutionStatus,
    TaskStage,
    TaskStatus,
)
from litehive.domain.outcomes import TaskOutcomeKind
from litehive.domain.task import TaskRecord, WorkspaceState
from litehive.tasks.failed_runs import has_blocking_failed_run_history
from litehive.tasks.normalization import implementation_entry_stage
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace

_TERMINAL_EXECUTION_STATUSES = {
    TaskExecutionStatus.DONE,
    TaskExecutionStatus.CANCELLED,
    TaskExecutionStatus.FAILED,
    TaskExecutionStatus.BLOCKED,
    TaskExecutionStatus.INTERRUPTED,
}
_TERMINAL_OUTCOME_KINDS = {
    TaskOutcomeKind.CLOSED,
    TaskOutcomeKind.DUPLICATE,
    TaskOutcomeKind.DEFERRED,
    TaskOutcomeKind.WONT_DO,
}
_TRUSTED_STAGE_MARKER_STATUSES = {
    RuntimeStageStatus.IDLE,
    "paused",
    RuntimeStageStatus.INTERRUPTED,
    RuntimeStageStatus.RUNNING,
}
_RESUMABLE_PIPELINE_STAGES: frozenset[TaskStage] = frozenset(
    {TaskStage.GROOMING, TaskStage.IMPLEMENTING, TaskStage.TESTING, TaskStage.ACCEPTING, TaskStage.COMMIT_TO_GIT}
)


def _normalize_resumable_stage_name(stage: str | None) -> str | None:
    """
    Filter a candidate stage label to the set of legitimate resume targets.

    ``resumable_queue_stage`` walks several stage hints through this so a
    stale ``recovering`` or ``backlog`` value can never be treated as a
    resume target — only grooming/implementing/testing/accepting/commit
    stages may resume in place.
    """
    if stage in _RESUMABLE_PIPELINE_STAGES:
        return stage
    return None


def resumable_queue_stage(task: TaskRecord) -> str | None:
    """
    Pick the stage a queued/idle task should resume at, or ``None`` to restart.

    Used by execution recovery and the ``resume`` lifecycle command to decide
    whether an interrupted task can pick up mid-pipeline (grooming through
    commit) instead of being demoted back to ``backlog`` and restarting.
    """
    interruption = task.runtime.execution.interruption
    current_stage = task.runtime.pipeline.current_stage
    if interruption is None:
        interruption_resume_stage = None
        interruption_pipeline_status = None
    else:
        interruption_resume_stage = interruption.resume_stage
        interruption_pipeline_status = interruption.pipeline_status
    candidates = [
        task.pipeline_status,
        interruption_resume_stage,
        interruption_pipeline_status,
    ]
    if current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        candidates.append(current_stage.stage)
    for candidate in candidates:
        normalized = _normalize_resumable_stage_name(candidate)
        if normalized is not None:
            return normalized
    return None


def resumable_running_stage(task: TaskRecord) -> str | None:
    """
    Pick the resume stage for a task whose runtime still claims ``running``.

    Stale-runner recovery uses this once it has decided a "running" task is
    actually orphaned (e.g. subagent PID dead): trusts the live current-stage
    marker first because that's the most recent stage hint, then falls back
    to the queued-stage heuristics.
    """
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.status == RuntimeStageStatus.RUNNING:
        normalized = _normalize_resumable_stage_name(current_stage.stage)
        if normalized is not None:
            return normalized
    return resumable_queue_stage(task)


def _needs_manual_intervention(task: TaskRecord) -> bool:
    """
    True when the task has tripped enough flags to require operator review.

    Either the flag count crossed the auto-defer threshold or the flag
    reason is in the sticky set (hook reject loop, semantic reject, time
    budget exceeded, etc.); ``is_task_eligible_for_execution`` uses this
    to keep such tasks out of the queue.
    """
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
    """
    True when the task has run out of automatic-recovery budget.

    The flag reasons handled here (crash budget exhausted, recovery budget
    exhausted, recovery failed) all mean the recovery agent has nothing
    left to try; the queue selector treats them as terminal so it does not
    burn cycles re-launching the recovery agent on a hopeless task.
    """
    return task.status == TaskStatus.FLAGGED and task.flag_reason in {
        "crash_budget_exhausted",
        "recovery_budget_exhausted",
        "recovery_failed",
    }


def _should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    """
    True when a commit-stage task is in a recoverable status.

    Used by the auto-recovery requeue path so a task that crashed mid-merge
    resumes at the commit stage instead of restarting from scratch and
    re-doing the implementation/testing work that already passed.
    """
    return task.pipeline_status == PipelineStatus.COMMIT_TO_GIT and task.status in {
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.INTERRUPTED,
    }


def _has_terminal_execution_status(task: TaskRecord) -> bool:
    """
    True when the runtime pipeline reports a terminal execution status.

    Done, cancelled, failed, blocked, and interrupted all qualify; the
    short-circuit in ``is_task_eligible_for_execution`` prevents a task
    whose runner already finished from accidentally re-queueing itself
    on the next selection pass.
    """
    return task.runtime.pipeline.execution_status in _TERMINAL_EXECUTION_STATUSES


def _has_terminal_outcome_kind(task: TaskRecord) -> bool:
    """
    True when the last pipeline outcome was a terminal closure verdict.

    Closed, duplicate, deferred, and won't-do all qualify; this short-circuit
    keeps tasks whose acceptor already decided they should not run again
    out of the queue selector even when their lifecycle status has not yet
    been bumped to match.
    """
    kind = task.runtime.pipeline.last_outcome.kind
    return kind is not None and kind in _TERMINAL_OUTCOME_KINDS


def task_has_resume_marker(task: TaskRecord) -> bool:
    """
    Tell whether a task's runtime still vouches for its declared pipeline stage.

    The stale-pipeline normalizer and the workspace-repair pass that mirrors
    it consult this before demoting a queued task back to ``backlog``: a
    task with a trustworthy stage marker or a matching interruption record
    must be left alone so it can resume in place rather than restart.
    """
    stage = task.pipeline_status
    current_stage = task.runtime.pipeline.current_stage
    if current_stage.stage == stage and current_stage.status in _TRUSTED_STAGE_MARKER_STATUSES:
        return True
    interruption = task.runtime.execution.interruption
    if interruption is not None and (interruption.resume_stage == stage or interruption.pipeline_status == stage):
        return True
    return False


def _is_parked_task(task: TaskRecord) -> bool:
    """
    True when an operator has explicitly parked the task.

    Queue-selection helpers branch on this so parked tasks don't show up in
    pickable lists even if their pipeline status looks runnable; an operator
    has to actively un-park before the task can be dequeued.
    """
    return task.status == TaskStatus.PARKED


def is_task_eligible_for_execution(task: TaskRecord) -> bool:
    """
    Decide whether a task may be dequeued or kept active right now.

    The single source of truth for "is this task runnable?" — the queue
    selector, workspace-repair flows, status diagnostics, and the
    single-active-task invariant in the workspace lock all consult this so
    they share one view of terminal/blocked statuses.
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


def _auto_recovery_stage_for_flagged_task(task: TaskRecord) -> PipelineStatus:
    """
    Pick the resume stage when a flagged task is rescued back into the queue.

    Keeps a flagged-mid-commit task at ``commit_to_git`` so it doesn't
    re-do the implementation; otherwise drops the task back to the
    implementation entry stage so the agent can rebuild the change set
    from a known-good baseline.
    """
    if task.pipeline_status == PipelineStatus.COMMIT_TO_GIT:
        return PipelineStatus.COMMIT_TO_GIT
    return implementation_entry_stage(task)


def _is_task_completed(task: TaskRecord) -> bool:
    """
    True when both the lifecycle status and the pipeline status say DONE.

    Used by the dependency walk: a half-done task (e.g. lifecycle done but
    pipeline still ongoing) must not satisfy a dependency edge or downstream
    tasks would launch before the upstream work is actually committed.
    """
    return task.status == TaskStatus.DONE and task.pipeline_status == PipelineStatus.DONE


def _task_blockers(task: TaskRecord, tasks_by_id: dict[str, TaskRecord]) -> list[str]:
    """
    Return human-readable labels for unmet or missing dependencies.

    The queue selector uses this to explain why a task is blocked in
    status output without re-walking the dependency graph for every
    affected task; one-pass labelling keeps blocked-task rendering O(d)
    in the number of dependencies.
    """
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


class TaskDependencyValidator:
    """
    Workspace-bound validator for task dependency edges.

    Refuses self-referential, missing, or cyclic ``depends_on`` lists before
    they reach persistence, so queue selection never has to defend against
    corrupt dependency graphs.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def validate(self, task_id: str, depends_on: list[str]) -> None:
        """
        Reject invalid dependencies for ``task_id``.

        Called when persisting a new task and when ``litehive update``
        rewrites a task's dependency list.
        """
        tasks_by_id = {task.id: task for task in WorkspaceTasks(self.workspace).list(strict=False)}
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
    """
    Cycle check helper used by ``validate_task_dependencies``.

    Walks the dependency graph from ``dependency_id`` and returns True when
    it transitively reaches ``task_id``, so a self-cycle (direct or indirect)
    can be rejected before the corrupt edge is persisted. Iterative depth
    walk to avoid recursion limits on long dependency chains.
    """
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
    """
    True when an eligible task is mid-pipeline rather than fresh out of backlog.

    Queue-selection prefers these tasks when picking the next runner so
    paused work resumes before new work starts; without the preference, a
    long-paused task could be passed over indefinitely in favour of fresh
    backlog candidates.
    """
    return is_task_eligible_for_execution(task) and (
        task.status == TaskStatus.IN_PROGRESS or task.pipeline_status != PipelineStatus.BACKLOG
    )


def _live_active_pipeline_stage(state: WorkspaceState, tasks_by_id: dict[str, TaskRecord]) -> str | None:
    """
    Return the live (running) stage of the workspace's active task.

    ``None`` when no task is actively on-CPU; the queue selector consults
    this so the stale-pipeline normaliser does not reset stages that are
    actively executing on the live runner.
    """
    if state.active_task_id is None:
        return None
    active_task = tasks_by_id.get(state.active_task_id)
    if active_task is None:
        return None
    current_stage = active_task.runtime.pipeline.current_stage
    if (
        active_task.runtime.pipeline.execution_status == TaskExecutionStatus.RUNNING
        or current_stage.status == RuntimeStageStatus.RUNNING
    ):
        return current_stage.stage or active_task.pipeline_status
    return None
