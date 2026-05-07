from typing import get_args

from litehive.domain.common import (
    PipelineState,
    PipelineStatus,
    SubagentStatus,
    TaskStage,
    Verdict,
    pipeline_status_for_pipeline_state,
    task_stage_for_pipeline_state,
)
from litehive.domain.failure_diagnostics import FailureDiagnostics
from litehive.domain.outcomes import OutcomeReasonCode, TaskCloseReason
from litehive.domain.reports import ReportPipelineState, StageReport, canonical_stage_report_verdict
from litehive.domain.runtime import RuntimeSubagentState, Subagent


def test_pipeline_state_is_canonical_machine_state_not_pipeline_status_alias() -> None:
    assert PipelineState is not PipelineStatus
    assert PipelineState.AFTER_IMPLEMENTING == "after_implementing"
    assert "after_implementing" not in {status.value for status in PipelineStatus}


def test_pipeline_state_projection_to_user_facing_stage_is_explicit() -> None:
    assert PipelineState.AFTER_IMPLEMENTING.task_stage == TaskStage.IMPLEMENTING
    assert PipelineState.READY.task_stage is None
    assert task_stage_for_pipeline_state(PipelineState.AFTER_IMPLEMENTING) == "implementing"


def test_pipeline_status_is_operator_facing_projection_not_machine_state() -> None:
    assert PipelineState.BEFORE_IMPLEMENTING.pipeline_status is PipelineStatus.IMPLEMENTING
    assert pipeline_status_for_pipeline_state(PipelineState.BEFORE_IMPLEMENTING) == PipelineStatus.IMPLEMENTING
    assert pipeline_status_for_pipeline_state(PipelineState.AFTER_IMPLEMENTING) == PipelineStatus.IMPLEMENTING
    assert pipeline_status_for_pipeline_state(PipelineState.FAILED) == PipelineStatus.FLAGGED


def test_pipeline_state_primary_stage_is_domain_behavior() -> None:
    assert PipelineState.BEFORE_IMPLEMENTING.primary_stage is PipelineState.IMPLEMENTING
    assert PipelineState.AFTER_COMMIT.primary_stage is PipelineState.COMMIT
    assert PipelineState.RECOVERING.primary_stage is PipelineState.GROOMING
    assert PipelineState.TESTING.primary_stage is PipelineState.TESTING


def test_task_stage_owner_role_is_domain_behavior() -> None:
    assert TaskStage.GROOMING.owner_role == "planner"
    assert TaskStage.IMPLEMENTING.owner_role == "swe"
    assert TaskStage.TESTING.owner_role == "qa"
    assert TaskStage.ACCEPTING.owner_role == "reviewer"
    assert TaskStage.COMMIT_TO_GIT.owner_role == "runner"


def test_task_stage_retry_counter_state_is_domain_behavior() -> None:
    assert TaskStage.GROOMING.retry_counter_state is PipelineState.GROOMING
    assert TaskStage.IMPLEMENTING.retry_counter_state is PipelineState.IMPLEMENTING
    assert TaskStage.TESTING.retry_counter_state is PipelineState.TESTING
    assert TaskStage.ACCEPTING.retry_counter_state is PipelineState.ACCEPTING
    assert TaskStage.COMMIT_TO_GIT.retry_counter_state is PipelineState.COMMIT


def test_every_pipeline_state_has_pipeline_status_projection() -> None:
    assert {state.pipeline_status for state in PipelineState} == {
        PipelineStatus.BACKLOG,
        PipelineStatus.GROOMING,
        PipelineStatus.IMPLEMENTING,
        PipelineStatus.TESTING,
        PipelineStatus.ACCEPTING,
        PipelineStatus.COMMIT_TO_GIT,
        PipelineStatus.DONE,
        PipelineStatus.FLAGGED,
    }


def test_stage_report_verdict_vocabulary_canonicalizes_to_pass_reject_blocked() -> None:
    assert set(get_args(StageReport.model_fields["verdict"].annotation)) == {"pass", "reject", "blocked"}
    assert Verdict.ACCEPT.stage_report_verdict == "pass"
    assert Verdict.FAIL.stage_report_verdict == "reject"
    assert Verdict.BUDGET_HIT.stage_report_verdict == "blocked"
    assert Verdict.COMMENT.stage_report_verdict is None
    assert canonical_stage_report_verdict("accept") == "pass"
    assert canonical_stage_report_verdict("fail") == "reject"
    assert canonical_stage_report_verdict("budget_hit") == "blocked"


def test_task_close_reason_owns_close_projection() -> None:
    assert TaskCloseReason.DONE.outcome_reason_code == "task_done"
    assert TaskCloseReason.DONE.task_close_label == "Task already satisfied."
    assert TaskCloseReason.DUPLICATE.outcome_reason_code == "task_closed"
    assert TaskCloseReason.DUPLICATE.task_close_label == "Task closed as duplicate."


def test_execution_interrupted_and_cancelled_reason_codes_are_distinct() -> None:
    assert OutcomeReasonCode.EXECUTION_INTERRUPTED != OutcomeReasonCode.EXECUTION_CANCELLED
    assert OutcomeReasonCode.EXECUTION_INTERRUPTED == "execution_interrupted"
    assert OutcomeReasonCode.EXECUTION_CANCELLED == "execution_cancelled"


def test_stage_report_pipeline_state_uses_named_report_projection() -> None:
    assert StageReport.model_fields["pipeline_state"].annotation == ReportPipelineState
    assert get_args(ReportPipelineState)[0] is TaskStage
    report = StageReport(task_id="T-0001", pipeline_state="implementing", verdict="pass", summary="ok")
    assert report.pipeline_state == "implementing"


def test_stage_report_failure_diagnostics_are_typed_but_serialize_as_object() -> None:
    report = StageReport(
        task_id="T-0001",
        pipeline_state="implementing",
        verdict="reject",
        summary="hook failed",
        failure_diagnostics={
            "phase": "after_commit",
            "consecutive_same_hook_rejects": 2,
            "claimed_files_changed": ["src/app.py"],
        },
    )

    assert isinstance(report.failure_diagnostics, FailureDiagnostics)
    assert report.failure_diagnostics["phase"] == "after_commit"
    assert report.failure_diagnostics.get("consecutive_same_hook_rejects") == 2
    assert report.model_dump(mode="json")["failure_diagnostics"] == {
        "phase": "after_commit",
        "consecutive_same_hook_rejects": 2,
        "claimed_files_changed": ["src/app.py"],
    }


def test_subagent_status_is_domain_enum_with_heru_serialized_values() -> None:
    assert SubagentStatus.RUNNING == "running"
    ref = Subagent(
        id="SA-0001",
        role="swe",
        engine="codex",
        status=SubagentStatus.RUNNING,
        path="subagents/SA-0001-swe",
    )
    runtime_state = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status=SubagentStatus.INTERRUPTED,
        path="subagents/SA-0001-swe",
        started_at="2026-05-07T00:00:00Z",
        updated_at="2026-05-07T00:00:01Z",
    )

    assert ref.model_dump(mode="json")["status"] == "running"
    assert runtime_state.model_dump(mode="json")["status"] == "interrupted"
