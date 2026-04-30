"""Recovery report application service."""

from pathlib import Path

from litehive.domain.recovery import TriggerEventKind
from litehive.domain.reports import RecoveryAction, RecoveryReport, TaskActivityEntry
from litehive.domain.task import TaskRecord
from litehive.tasks.activity import append_task_activity
from litehive.tasks.recovery_evidence import collect_recovery_evidence, stage_report_context
from litehive.tasks.report_storage import ReportReference, insert_recovery_report, latest_stage_report


def record_recovery_report(
    root: Path,
    task: TaskRecord,
    *,
    trigger_event_kind: TriggerEventKind,
    origin_stage: str | None,
    summary: str,
    runnable_state: str,
    actions: list[RecoveryAction] | None = None,
    failure_classification: str | None = None,
    blocker: str | None = None,
    warnings: list[str] | None = None,
) -> ReportReference:
    report = RecoveryReport(
        task_id=task.id,
        origin_stage=origin_stage,
        trigger_event_kind=trigger_event_kind,
        summary=summary,
        failure_classification=failure_classification,
        runnable_state=runnable_state,  # type: ignore[arg-type]
        blocker=blocker,
        evidence=collect_recovery_evidence(root, task, stage=origin_stage),
        actions=list(actions or []),
        warnings=list(warnings or []),
    )
    ref = insert_recovery_report(root, task, report)
    latest_report = latest_stage_report(root, task)
    append_task_activity(
        root,
        task,
        TaskActivityEntry(
            role="recovery",
            stage=origin_stage or task.pipeline_status,
            verdict="comment",
            message=(
                f"Recovery trigger `{trigger_event_kind.value}`: {summary}\n"
                f"runnable_state: {runnable_state}\n"
                f"report: {ref}"
                + (f"\nlatest_stage_report: {stage_report_context(latest_report)}" if latest_report is not None else "")
                + (f"\nblocker: {blocker}" if blocker else "")
            ),
        ),
    )
    return ref
