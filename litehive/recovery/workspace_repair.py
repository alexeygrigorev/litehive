"""Workspace repair entrypoint used by the daemon and repair CLI."""

from __future__ import annotations

from pathlib import Path

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.domain.task_ops import WorkspaceRepairSummary
from litehive.state.locking import workspace_lock
from litehive.state.persist import load_state, persist_task_and_state_without_runner_guard
from litehive.state.records import get_task_record
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.queue import idle_stage_state
from litehive.tasks.report_storage import latest_stage_report
from litehive.tasks.runtime import apply_task_outcome, clear_task_run_activity

from .execution_recovery import recover_stale_runner_state


_TERMINAL_REPAIR_STAGES = {"accepting", "commit_to_git"}


def repair_workspace_state(root: Path) -> WorkspaceRepairSummary:
    summary = WorkspaceRepairSummary()
    summary.stale_runner_recovered = recover_stale_runner_state(root, summary=summary)
    summary.mutated = summary.stale_runner_recovered
    normalized = _normalize_stale_terminal_tasks(root, summary=summary)
    summary.mutated = summary.mutated or normalized
    return summary


def _normalize_stale_terminal_tasks(root: Path, *, summary: WorkspaceRepairSummary | None = None) -> bool:
    mutated = False
    with workspace_lock(root):
        state = load_state(root, bootstrap=False)
        queued_ids = set(state.queue)
        for task_id in _stale_terminal_candidate_ids(root):
            if state.active_task_id == task_id or task_id in queued_ids:
                continue
            task = get_task_record(root, task_id)
            if task is None:
                continue
            report = latest_stage_report(root, task)
            if report is None or report.verdict != "pass" or report.pipeline_state not in _TERMINAL_REPAIR_STAGES:
                continue
            before_task = snapshot_task_audit_state(task)
            queue_before = list(state.queue)
            now = utcnow()
            task.status = "done"
            task.close_reason = "done"
            task.flag_reason = None
            task.pipeline_status = "done"
            clear_task_run_activity(task, execution_status="done", updated_at=now, clear_interruption=True)
            task.runtime.pipeline.current_stage = idle_stage_state(updated_at=now, stage="done")
            apply_task_outcome(
                task,
                kind="done",
                stage="done",
                reason_code="done",
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


def _stale_terminal_candidate_ids(root: Path) -> list[str]:
    with connect_workspace_db(root) as connection:
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
              AND COALESCE(
                    json_extract(task_state.payload, '$.runtime.pipeline.execution_status'),
                    json_extract(task_state.payload, '$.runtime.execution_status'),
                    'idle'
                  ) != 'running'
              AND json_extract(latest_stage_report.payload, '$.verdict') = 'pass'
              AND json_extract(latest_stage_report.payload, '$.pipeline_state') IN ('accepting', 'commit_to_git')
            ORDER BY task_state.task_id
            """
        ).fetchall()
    return [str(row["task_id"]) for row in rows]


__all__ = [
    "repair_workspace_state",
]
