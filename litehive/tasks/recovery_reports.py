"""Recovery report application service."""

from typing import Literal

from litehive.domain.common import Verdict
from litehive.domain.recovery import TriggerEventKind
from litehive.domain.reports import RecoveryAction, RecoveryReport, TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.tasks.activity import task_activity_store_for_task
from litehive.tasks.recovery_evidence import collect_recovery_evidence, stage_report_context
from litehive.tasks.report_storage import ReportReference, TaskReportStore
from litehive.workspace import Workspace


RecoveryRunnableState = Literal["runnable", "parked", "blocked"]


def record_recovery_report(
    workspace: Workspace,
    task: TaskRecord,
    trigger_event_kind: TriggerEventKind,
    origin_stage: str | None,
    summary: str,
    runnable_state: RecoveryRunnableState,
    actions: list[RecoveryAction] | None = None,
    failure_classification: str | None = None,
    blocker: str | None = None,
    warnings: list[str] | None = None,
) -> ReportReference:
    """
    Persist the recovery agent's verdict.

    Writes the report itself plus a matching task-activity entry so the
    report viewer and the activity timeline see the same trigger,
    runnable-state, and blocker. Called by the recovery stage when its
    agent finishes a triage pass.
    """
    report = RecoveryReport(
        task_id=task.id,
        origin_stage=origin_stage,
        trigger_event_kind=trigger_event_kind,
        summary=summary,
        failure_classification=failure_classification,
        runnable_state=runnable_state,
        blocker=blocker,
        evidence=collect_recovery_evidence(workspace, task, stage=origin_stage),
        actions=list(actions or []),
        warnings=list(warnings or []),
    )
    reports = TaskReportStore(workspace)
    ref = reports.insert_recovery_report(task, report)
    latest_report = reports.latest_stage_report(task)
    if latest_report is not None:
        latest_report_part = f"\nlatest_stage_report: {stage_report_context(latest_report)}"
    else:
        latest_report_part = ""
    if blocker:
        blocker_part = f"\nblocker: {blocker}"
    else:
        blocker_part = ""
    task_activity_store_for_task(workspace, task).append(
        TaskActivityEntry(
            source="system",
            role="recovery",
            stage=origin_stage or task.pipeline_status,
            verdict=Verdict.COMMENT,
            message=(
                f"Recovery trigger `{trigger_event_kind.value}`: {summary}\n"
                f"runnable_state: {runnable_state}\n"
                f"report: {ref}"
                + latest_report_part
                + blocker_part
            ),
        )
    )
    return ref
