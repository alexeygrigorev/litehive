"""Stage report parsing from subagent results."""

from litehive.domain.reports import (
    REPORT_VERDICT_KINDS,
    ReportPipelineState,
    StageReport,
    canonical_report_pipeline_state,
    canonical_stage_report_verdict,
)
from litehive.domain.common import Verdict
from litehive.domain.task import TaskRecord

from litehive.domain.agent import SubagentResult
from litehive.tasks.activity import latest_task_activity_entry
from litehive.workspace import Workspace


class MissingVerdictError(Exception):
    """The subagent finished its turn without submitting a verdict.

    Raised by :func:`stage_report_from_subagent` when no
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

    def __init__(self, *, pipeline_state: ReportPipelineState, subagent_id: str) -> None:
        self.pipeline_state = pipeline_state
        self.subagent_id = subagent_id
        super().__init__(
            f"subagent {subagent_id} finished {pipeline_state} without submitting a verdict via litehive agent report"
        )


def stage_report_from_subagent(
    task: TaskRecord,
    stage: ReportPipelineState,
    result: SubagentResult,
    workspace: Workspace,
) -> StageReport:
    """
    Build a :class:`StageReport` for a single subagent run.

    The agent submits its verdict via ``litehive agent report``,
    which appends a :class:`TaskActivityEntry`. This helper looks for
    that entry: present → the activity becomes the report; absent →
    we raise :class:`MissingVerdictError` so the caller can skip the
    stage-report row instead of persisting a synthetic reject. The
    real "kick the agent" flow lives in the lifecycle layer
    (``NudgeRequired``); this exception just keeps the recorded
    artifact honest about the fact that no verdict was submitted.
    """
    pipeline_state: ReportPipelineState = canonical_report_pipeline_state(stage)
    latest = latest_task_activity_entry(
        workspace,
        task,
        stage=pipeline_state,
        source_subagent_id=result.ref.id,
        verdicts=REPORT_VERDICT_KINDS,
    )
    if latest is None:
        raise MissingVerdictError(pipeline_state=pipeline_state, subagent_id=result.ref.id)

    if latest.verdict == Verdict.REJECT:
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
