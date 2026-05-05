"""Stage report parsing from subagent results."""

from pathlib import Path

from litehive.domain.common import cap_feedback
from litehive.domain.reports import (
    REPORT_VERDICT_KINDS,
    ReportPipelineState,
    StageReport,
    canonical_report_pipeline_state,
    canonical_stage_report_verdict,
)
from litehive.domain.task import TaskRecord

from litehive.domain.agent import SubagentResult
from litehive.tasks.activity import latest_task_activity_entry
from litehive.workspace import Workspace


def stage_report_from_subagent(
    task: TaskRecord,
    stage: ReportPipelineState,
    result: SubagentResult,
    root: Path,
) -> StageReport:
    """
    Build a :class:`StageReport` for a single subagent run.

    The agent submits its verdict via ``litehive agent report``,
    which appends a :class:`TaskActivityEntry`. This helper looks for
    that entry: present → the activity becomes the report; absent →
    the agent finished a turn without reporting and we construct a
    synthetic ``reject`` so the lifecycle can route the failure
    through the normal nudge / non-completion paths instead of
    treating silence as success.
    """
    pipeline_state: ReportPipelineState = canonical_report_pipeline_state(stage)
    latest = latest_task_activity_entry(
        Workspace.from_path(root),
        task,
        stage=pipeline_state,
        source_subagent_id=result.ref.id,
        verdicts=REPORT_VERDICT_KINDS,
    )
    if latest is None:
        return StageReport(
            task_id=task.id,
            pipeline_state=pipeline_state,
            verdict="reject",
            summary=f"{pipeline_state} rejected: agent did not submit verdict via litehive agent report CLI",
            feedback=cap_feedback(result.execution_trace),
            warnings=["Agent did not submit verdict via litehive agent report CLI."],
        )

    if latest.verdict == "reject":
        failure_classification = latest.verdict_classification
    else:
        failure_classification = None
    report_verdict = canonical_stage_report_verdict(latest.verdict) or "reject"
    if latest.message:
        summary = latest.message.splitlines()[0]
    else:
        summary = f"{pipeline_state} {latest.verdict}"
    if failure_classification:
        failure_diagnostics = {"verdict_classification": failure_classification, "role": latest.role}
    else:
        failure_diagnostics = {}
    return StageReport(
        task_id=task.id,
        pipeline_state=pipeline_state,
        verdict=report_verdict,
        summary=summary,
        feedback=latest.message,
        submitted_via_cli=True,
        failure_classification=failure_classification,
        failure_diagnostics=failure_diagnostics,
    )
