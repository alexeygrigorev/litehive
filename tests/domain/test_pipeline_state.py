from typing import get_args

from litehive.domain.common import (
    PipelineState,
    PipelineStatus,
    TaskStage,
    pipeline_status_for_pipeline_state,
    task_stage_for_pipeline_state,
)
from litehive.domain.reports import ReportPipelineState, StageReport, canonical_stage_report_verdict


def test_pipeline_state_is_canonical_machine_state_not_pipeline_status_alias() -> None:
    assert PipelineState is not PipelineStatus
    assert PipelineState.AFTER_IMPLEMENTING == "after_implementing"
    assert "after_implementing" not in {status.value for status in PipelineStatus}


def test_pipeline_state_projection_to_user_facing_stage_is_explicit() -> None:
    assert task_stage_for_pipeline_state(PipelineState.AFTER_IMPLEMENTING) == "implementing"


def test_pipeline_status_is_operator_facing_projection_not_machine_state() -> None:
    assert pipeline_status_for_pipeline_state(PipelineState.BEFORE_IMPLEMENTING) == PipelineStatus.IMPLEMENTING
    assert pipeline_status_for_pipeline_state(PipelineState.AFTER_IMPLEMENTING) == PipelineStatus.IMPLEMENTING
    assert pipeline_status_for_pipeline_state(PipelineState.FAILED) == PipelineStatus.FLAGGED


def test_stage_report_verdict_vocabulary_canonicalizes_to_pass_reject_blocked() -> None:
    assert set(get_args(StageReport.model_fields["verdict"].annotation)) == {"pass", "reject", "blocked"}
    assert canonical_stage_report_verdict("accept") == "pass"
    assert canonical_stage_report_verdict("fail") == "reject"
    assert canonical_stage_report_verdict("budget_hit") == "blocked"


def test_stage_report_pipeline_state_uses_named_report_projection() -> None:
    assert StageReport.model_fields["pipeline_state"].annotation == ReportPipelineState
    assert get_args(ReportPipelineState)[0] is TaskStage
    report = StageReport(task_id="T-0001", pipeline_state="implementing", verdict="pass", summary="ok")
    assert report.pipeline_state == "implementing"
