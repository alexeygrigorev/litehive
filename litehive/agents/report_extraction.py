"""Stage report extraction from subagent activity."""

from litehive.domain.reports import (
    FailureDiagnostics,
    REPORT_VERDICT_KINDS,
    ReportPipelineState,
    StageReport,
    TaskActivityEntry,
    canonical_report_pipeline_state,
    canonical_stage_report_verdict,
)
from litehive.domain.common import Verdict
from litehive.domain.task import TaskRecord

from litehive.domain.agent import SubagentId, SubagentResult
from litehive.tasks.activity import task_activity_store_for_task
from litehive.workspace import Workspace


class MissingVerdictError(Exception):
    """The subagent finished its turn without submitting a verdict.

    Raised by :meth:`AgentReportService.stage_report_from_subagent` when no
    :class:`TaskActivityEntry` exists for the run: the agent did not
    call ``litehive agent report``. This is an exceptional outcome
    rather than a recordable verdict — the lifecycle layer recovers
    by raising :class:`NudgeRequired` (a separate exception that
    drives the same-session nudge retry) so the agent gets another
    turn to actually report. Callers in the runner that just need to
    write an observability snapshot catch this and skip the
    ``record_stage_report`` row instead of persisting a synthetic
    reject that would lie about what happened.

    Carries the resolved ``pipeline_state`` and ``subagent_id`` so
    snapshot writers can describe the missing-verdict situation
    without re-deriving them.
    """

    def __init__(self, *, pipeline_state: ReportPipelineState, subagent_id: SubagentId) -> None:
        self.pipeline_state = pipeline_state
        self.subagent_id = subagent_id
        super().__init__(
            f"subagent {subagent_id} finished {pipeline_state} without submitting a verdict via litehive agent report"
        )


class AgentReportService:
    """
    Build stage reports from subagent activity submissions.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def stage_report_from_subagent(
        self,
        task: TaskRecord,
        stage: ReportPipelineState,
        result: SubagentResult,
    ) -> StageReport:
        """
        Build a :class:`StageReport` for a single subagent run.
        """
        pipeline_state: ReportPipelineState = canonical_report_pipeline_state(stage)
        latest = task_activity_store_for_task(self.workspace, task).latest_entry(
            stage=pipeline_state,
            source_subagent_id=SubagentId(result.ref.id),
            verdicts=REPORT_VERDICT_KINDS,
        )
        if latest is None:
            raise MissingVerdictError(pipeline_state=pipeline_state, subagent_id=SubagentId(result.ref.id))

        if latest.verdict == Verdict.REJECT:
            failure_classification = latest.verdict_classification
        else:
            failure_classification = None
        report_verdict = canonical_stage_report_verdict(latest.verdict) or "reject"
        summary = _stage_report_summary(latest, pipeline_state)
        failure_diagnostics = _failure_diagnostics_for_activity(latest, failure_classification)
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


def _stage_report_summary(latest: TaskActivityEntry, pipeline_state: ReportPipelineState) -> str:
    """
    Return the one-line summary persisted on a stage report.
    """
    if latest.message:
        return latest.message.splitlines()[0]
    return f"{pipeline_state} {latest.verdict}"


def _failure_diagnostics_for_activity(
    latest: TaskActivityEntry,
    failure_classification: str | None,
) -> FailureDiagnostics:
    """
    Return typed diagnostics derived from a classified activity entry.
    """
    if failure_classification is None:
        return FailureDiagnostics({})
    return FailureDiagnostics({"verdict_classification": failure_classification, "role": latest.role})
