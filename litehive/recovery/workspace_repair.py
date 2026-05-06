"""Workspace repair entrypoint used by the daemon and repair CLI."""

from litehive.domain.common import (
    OutcomeKind,
    OutcomeReasonCode,
    PipelineStatus,
    TaskStage,
    TaskStatus,
    utcnow,
)
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.state.locking import workspace_lock
from litehive.state.persist import load_state, persist_task_and_state_without_runner_guard
from litehive.state.records import get_task_record
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue import idle_stage_state
from litehive.tasks.report_storage import latest_stage_report
from litehive.tasks.runtime import apply_task_outcome, clear_task_run_activity
from litehive.workspace import Workspace

from .execution_recovery import recover_stale_runner_state


_TERMINAL_REPAIR_STAGES = frozenset({TaskStage.ACCEPTING, TaskStage.COMMIT_TO_GIT})


def repair_workspace_state(workspace: Workspace) -> WorkspaceRepairSummary:
    """
    Reconcile workspace runtime state after a crash or a forgotten terminal task.

    Called by the daemon startup path and the ``litehive repair`` CLI;
    runs stale-runner recovery first so the rest of the repair sees a
    clean active-task pointer, then promotes any task whose latest
    pass report shows it actually finished.
    """
    summary = WorkspaceRepairSummary()
    summary.stale_runner_recovered = recover_stale_runner_state(workspace.root, summary=summary)
    summary.mutated = summary.stale_runner_recovered
    normalized = _normalize_stale_terminal_tasks(workspace, summary=summary)
    summary.mutated = summary.mutated or normalized
    return summary


def _normalize_stale_terminal_tasks(workspace: Workspace, summary: WorkspaceRepairSummary | None = None) -> bool:
    """
    Promote tasks that already passed accepting/commit-to-git to ``DONE``.

    Covers the race where a daemon crash left a successful task pinned
    in the queue: the latest stage report says the task is finished
    but the runtime row never caught up. The operator-visible repair
    summary reports each touched task id so the action is auditable.
    """
    mutated = False
    root = workspace.root
    with workspace_lock(root):
        state = load_state(root, bootstrap=False)
        queued_ids = set(state.queue)
        for task_id in _stale_terminal_candidate_ids(workspace):
            if state.active_task_id == task_id or task_id in queued_ids:
                continue
            task = get_task_record(root, task_id)
            if task is None:
                continue
            report = latest_stage_report(workspace, task)
            if report is None or report.verdict != "pass" or report.pipeline_state not in _TERMINAL_REPAIR_STAGES:
                continue
            before_task = snapshot_task_audit_state(task)
            queue_before = list(state.queue)
            now = utcnow()
            task.status = TaskStatus.DONE
            task.close_reason = "done"
            task.flag_reason = None
            task.pipeline_status = PipelineStatus.DONE
            clear_task_run_activity(task, execution_status="done", updated_at=now, clear_interruption=True)
            task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now, stage="done")
            apply_task_outcome(
                task,
                kind=OutcomeKind.DONE,
                stage="done",
                reason_code=OutcomeReasonCode.DONE,
                reason="Recovered stale terminal task state from the latest pass report.",
                retry_count=task.runtime.pipeline.retry_count,
                retry_limit=task.runtime.pipeline.retry_limit,
            )
            state.queue = [queued_id for queued_id in state.queue if queued_id != task.id]
            if state.active_task_id == task.id:
                state.active_task_id = None
            persist_task_and_state_without_runner_guard(
                root,
                task=task,
                state=state,
                journal_message=(
                    "Recovered stale terminal task state from the latest pass report; "
                    "marked the task done instead of leaving it queued."
                ),
                audit_entries=[
                    build_task_audit_entry(
                        task_id=task.id,
                        action="reconciled",
                        actor="system",
                        source="repair",
                        before_task=before_task,
                        after_task=task,
                        before_queue=queue_before,
                        after_queue=state.queue,
                        context={
                            "pipeline_state": report.pipeline_state,
                            "verdict": report.verdict,
                            "source": report.source,
                            "stage_report_created_at": report.created_at,
                        },
                    )
                ],
            )
            if summary is not None and task.id not in summary.terminal_task_ids:
                summary.terminal_task_ids.append(task.id)
            mutated = True
    if summary is not None and mutated:
        summary.mutated = True
    return mutated


def _stale_terminal_candidate_ids(workspace: Workspace) -> list[str]:
    """
    SQL-side filter for tasks whose latest report passed but whose row didn't.

    Returns task ids whose latest stage report passed at accepting or
    commit-to-git while the runtime row is still
    queued/in-progress/interrupted; pre-filtering in the database
    keeps the Python reconciliation loop short on busy workspaces
    instead of walking every task.
    """
    with workspace.connect() as connection:
        rows = connection.execute(
            """
            WITH latest_stage_report AS (
                SELECT stage_reports.task_id, stage_reports.payload
                FROM stage_reports
                JOIN (
                    SELECT task_id, MAX(id) AS latest_id
                    FROM stage_reports
                    GROUP BY task_id
                ) AS latest
                    ON latest.task_id = stage_reports.task_id
                   AND latest.latest_id = stage_reports.id
            )
            SELECT task_state.task_id
            FROM task_state
            JOIN latest_stage_report
                ON latest_stage_report.task_id = task_state.task_id
            WHERE json_extract(task_state.payload, '$.status') IN ('queued', 'in_progress', 'interrupted')
              AND json_extract(task_state.payload, '$.pipeline_status') != 'done'
              AND COALESCE(json_extract(task_state.payload, '$.runtime.pipeline.execution_status'), 'idle') != 'running'
              AND json_extract(latest_stage_report.payload, '$.verdict') = 'pass'
              AND json_extract(latest_stage_report.payload, '$.pipeline_state') IN ('accepting', 'commit_to_git')
            ORDER BY task_state.task_id
            """
        ).fetchall()
    return [str(row["task_id"]) for row in rows]


__all__ = [
    "repair_workspace_state",
]
