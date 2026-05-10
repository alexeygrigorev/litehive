"""Per-task repair for tasks left running after a runner died.

The bulk of the stale-runner recovery loop: walks every task whose
runtime row still says ``running``, decides whether the lock holder is
genuinely live or wedged, and re-canonicalises wedged tasks back to a
resumable stage with a recovery report.
"""

from datetime import UTC, datetime
import sqlite3
from typing import TypedDict

from litehive.domain.common import PipelineStatus, TaskExecutionStatus, TaskStatus
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.reports import RecoveryAction
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.observability.events import last_event_timestamp
from litehive.recovery.interruption_state import (
    interruption_journal_message,
    prepare_interrupted_task,
    stale_interruption_reason,
)
from litehive.state.locking import (
    WorkspaceRunnerLock,
    runner_metadata_present,
    subagent_process_is_stale,
)
from litehive.tasks.recovery_reports import record_recovery_report
from litehive.workspace import Workspace


class RunningTaskRecoveryResult(TypedDict):
    """
    Recovery summary returned by :func:`recover_running_tasks`.

    Lets the caller route post-mortem state (persist transitions,
    re-order the queue, render journal entries) without re-walking
    the task population.

    mutated is true when at least one task was repaired.
    transitioned holds every task record that changed.
    journal_messages maps task id to a human-readable journal line.
    prioritized_ids lists task ids that should run before fresh backlog.
    """

    mutated: bool
    transitioned: list[TaskRecord]
    journal_messages: dict[str, str]
    prioritized_ids: list[str]


def _stale_pid_warnings(stale_pid: bool) -> list[str]:
    """
    Return the warnings list attached to the recovery report.

    Used by ``_record_stale_recovery`` to flag a confirmed-dead subagent
    PID; surfaces an empty list (rather than a placeholder string) when
    no stale PID was seen so the report renderer doesn't have to filter
    out a literal "(none)" entry.
    """
    if stale_pid:
        return ["stale subagent pid detected"]
    return []


def running_task_ids(workspace: Workspace) -> list[str]:
    """
    Pull every task id whose runtime row says ``execution_status='running'``.

    The cheap SQLite-side probe the recovery loop uses to decide
    whether the workspace looks wedged; loading the full task records
    just to check this would dwarf the cost of the recovery scan
    itself on busy workspaces.
    """
    with workspace.connect() as connection:
        try:
            rows = connection.execute(
                """
                SELECT task_id
                FROM task_state
                WHERE json_extract(payload, '$.runtime.pipeline.execution_status') = 'running'
                ORDER BY task_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [str(row["task_id"]) for row in rows]


def should_requeue_commit_stage_task(task: TaskRecord) -> bool:
    """
    True when a commit-stage task must be re-prioritised by repair.

    The commit-to-git stage is the only mid-pipeline stage where
    requeueing is safe even if the task wasn't the workspace's active
    task at the time of the crash; earlier stages may have unmerged
    work that re-running could double-claim.
    """
    return task.pipeline_status == PipelineStatus.COMMIT_TO_GIT and task.status in {
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.INTERRUPTED,
    }


def can_attempt_stale_runner_recovery(
    workspace: Workspace,
    tasks_by_id: dict[str, TaskRecord],
    running_task_ids: list[str],
) -> bool:
    """
    Gate that prevents stale-runner repair from racing a live runner.

    Only intervenes when the lock is unowned, the lock pid is dead, or
    the inactivity timeout proves the live runner is frozen; without
    the gate, repair could rewrite state for a runner that is just
    slow rather than actually stale.
    """
    if len(running_task_ids) > 1:
        return False
    runner_lock = WorkspaceRunnerLock(workspace)
    if not runner_lock.owns_current_thread() and runner_lock.is_held():
        if not runner_lock.pid_is_stale():
            config = workspace.load_config()
            if config.inactivity_timeout_seconds is None:
                return False
            if not _has_inactive_running_tasks(workspace, tasks_by_id, config.inactivity_timeout_seconds):
                return False
    return True


def recover_running_tasks(
    workspace: Workspace,
    state,
    tasks_by_id: dict[str, TaskRecord],
    running_task_ids: list[str],
    summary: WorkspaceRepairSummary | None,
) -> RunningTaskRecoveryResult:
    """
    Driver loop over every task whose runtime row says ``running``.

    Skips tasks that are not the workspace's active task unless the
    runner lock metadata is missing — in that case the row is by
    definition stale because there's no claim to compare against, so
    repairing it cannot conflict with a live owner.
    """
    mutated = False
    transitioned: list[TaskRecord] = []
    journal_messages: dict[str, str] = {}
    prioritized_ids: list[str] = []
    for task_id in running_task_ids:
        task = tasks_by_id.get(task_id)
        if task is None:
            continue
        if task_id != state.active_task_id and not should_requeue_commit_stage_task(task):
            if state.active_task_id is not None:
                metadata = WorkspaceRunnerLock(workspace).read_metadata()
                if not runner_metadata_present(metadata):
                    continue
        task_mutated, journal_message, prioritize = _recover_stale_running_task(workspace, task, summary=summary)
        if not task_mutated:
            continue
        transitioned.append(task)
        mutated = True
        if journal_message is not None:
            journal_messages[task.id] = journal_message
        if prioritize:
            prioritized_ids.append(task.id)
    return {
        "mutated": mutated,
        "transitioned": transitioned,
        "journal_messages": journal_messages,
        "prioritized_ids": prioritized_ids,
    }


def update_active_task_after_recovery(
    workspace: Workspace,
    state,
    tasks_by_id: dict[str, TaskRecord],
    prioritized_ids: list[str],
    running_task_ids: list[str],
    summary: WorkspaceRepairSummary | None,
) -> bool:
    """
    Reconcile ``state.active_task_id`` and queue ordering with just-repaired tasks.

    Clears ``active_task_id`` when the previously-active task is gone
    or has been requeued so the next runner does not latch onto a task
    it can no longer run; the prioritisation re-orders the queue so
    rescued in-flight work runs ahead of fresh backlog.
    """
    mutated = False
    if prioritized_ids:
        state.queue = [task_id for task_id in state.queue if task_id not in running_task_ids]
        state.queue = [*prioritized_ids, *state.queue]
        mutated = True
    if state.active_task_id is None:
        return mutated
    active_task = tasks_by_id.get(state.active_task_id)
    active_task_missing = state.active_task_id not in tasks_by_id and not _task_state_row_exists(
        workspace, state.active_task_id
    )
    should_clear_active_task_id = (
        active_task_missing
        or state.active_task_id in prioritized_ids
        or (
            active_task is not None
            and active_task.runtime.pipeline.execution_status != TaskExecutionStatus.RUNNING
            and active_task.id not in running_task_ids
            and not should_requeue_commit_stage_task(active_task)
        )
    )
    if should_clear_active_task_id:
        if (
            summary is not None
            and summary.cleared_active_task_id is None
            and not (active_task is not None and should_requeue_commit_stage_task(active_task))
        ):
            summary.cleared_active_task_id = state.active_task_id
        state.active_task_id = None
        mutated = True
    return mutated


def _has_inactive_running_tasks(
    workspace: Workspace,
    tasks_by_id: dict[str, TaskRecord],
    timeout_seconds: float,
) -> bool:
    """
    True when any "running" task has gone silent past the inactivity threshold.

    Lets the recovery scan act on a frozen runner whose lockfile pid
    is still alive but whose event stream has stopped — the
    pid-alive-but-deadlocked shape that simple PID checks cannot
    distinguish from a healthy long-running task.
    """
    for task in tasks_by_id.values():
        if task.runtime.pipeline.execution_status != TaskExecutionStatus.RUNNING:
            continue
        ts_str = last_event_timestamp(workspace, task)
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


def _record_stale_recovery(
    workspace: Workspace,
    task: TaskRecord,
    stage: str,
    journal_message: str,
    summary: WorkspaceRepairSummary | None,
    stale_pid: bool,
) -> None:
    """
    Emit the operator-facing recovery report and update the repair summary.

    A single repair pass is reflected both in the per-task report (so
    the operator can drill into one task) and in the aggregate "what
    did repair do?" view shown by the CLI; without the dual write,
    operators would see only one of the two surfaces.
    """
    record_recovery_report(
        workspace,
        task,
        trigger_event_kind=TriggerEventKind.STALE_RUNNER_RECOVERY,
        origin_stage=stage,
        summary=journal_message,
        runnable_state="runnable",
        failure_classification="stale_runner",
        actions=[
            RecoveryAction(
                action="clear_stale_active_state", summary="Cleared stale active runner state for the task."
            ),
            RecoveryAction(action="requeue_stage", summary=f"Requeued the task at {stage}.", metadata={"stage": stage}),
        ],
        warnings=_stale_pid_warnings(stale_pid),
    )
    if summary is not None and task.id not in summary.requeued_task_ids:
        summary.requeued_task_ids.append(task.id)
    if stale_pid and summary is not None and task.id not in summary.stale_process_task_ids:
        summary.stale_process_task_ids.append(task.id)


def _recover_stale_running_task(
    workspace: Workspace,
    task: TaskRecord,
    summary: WorkspaceRepairSummary | None,
) -> tuple[bool, str | None, bool]:
    """
    Run the per-task repair on one task still flagged ``running``.

    Re-canonicalises it onto its resumable stage, emits the recovery
    report, and returns ``(mutated, journal_message, prioritize)`` so
    the caller can update the queue order in one outer pass instead of
    persisting each task separately.
    """
    # inline: tasks.queue top-level-imports execution_recovery (would cycle).
    from litehive.tasks.queue import (  # noqa: PLC0415
        canonicalize_resumable_queue_task,
        is_task_eligible_for_execution,
        resumable_running_stage,
    )

    if not is_task_eligible_for_execution(task):
        return False, None, False
    stale_pid = subagent_process_is_stale(task)
    stage = resumable_running_stage(task)
    if stage is None:
        return False, None, False
    prepare_interrupted_task(
        workspace,
        task,
        stage=stage,
        summary=f"Interrupted run recovered after stale runner detection. Resume from `{stage}`.",
        reason=stale_interruption_reason(task, stage, stale_pid=stale_pid),
    )
    canonicalize_resumable_queue_task(task, stage=stage)
    _record_stale_recovery(
        workspace,
        task,
        stage=stage,
        journal_message=f"Recovered stale runner state and returned the task to `{stage}`.",
        summary=summary,
        stale_pid=stale_pid,
    )
    return True, interruption_journal_message(task), True


def _task_state_row_exists(workspace: Workspace, task_id: str) -> bool:
    """
    Existence probe distinguishing "task deleted" from "task in a bad state".

    Lets the active-task reconciliation safely clear
    ``state.active_task_id`` only when there is genuinely no row to
    point at anymore; without the distinction, a temporarily-corrupt
    task would be wrongly treated as gone.
    """
    with workspace.connect() as connection:
        try:
            row = connection.execute(
                "SELECT 1 FROM task_state WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    return row is not None
