from pathlib import Path

from heru.types import SubagentRef

from litehive.config.workspace import create_workspace
from litehive.domain.outcomes import OutcomeReasonCode, TaskOutcomeKind
from litehive.domain.failure_diagnostics import FailureDiagnostics
from litehive.domain.reports import StageReport
from litehive.domain.runtime import RuntimeInterruptionState, RuntimeStageState
from litehive.state.records import create_task, require_task, save_task
from litehive.tasks.runtime import (
    mark_stage_finished_for_workspace,
    mark_stage_started_for_workspace,
    mark_subagent_finished,
    mark_subagent_started,
    mark_task_outcome_for_workspace,
    mark_task_run_started_for_workspace,
)
from litehive.workspace import Workspace


def _subagent_ref() -> SubagentRef:
    return SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
    )


def _assert_runtime_stage_has_no_removed_fields(stage: RuntimeStageState) -> None:
    for field_name in ("completed_at", "verdict", "summary"):
        assert not hasattr(stage, field_name)


def test_runtime_stage_model_copy_does_not_resurrect_removed_fields() -> None:
    stage = RuntimeStageState(stage="testing").model_copy(
        update={"completed_at": "x", "verdict": "blocked", "summary": "old", "updated_at": "now"}
    )

    assert stage.updated_at == "now"
    _assert_runtime_stage_has_no_removed_fields(stage)


def test_mark_task_run_started_resets_stage_and_active_subagent(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Reset runtime")

    workspace = Workspace.from_path(tmp_path)

    mark_stage_started_for_workspace(workspace, task, "implementing")
    task = require_task(tmp_path, task.id)
    mark_subagent_started(tmp_path, task, _subagent_ref())

    task = require_task(tmp_path, task.id)
    task.runtime.execution.interruption = RuntimeInterruptionState(source="runner", reason="stale state")
    save_task(tmp_path, task)

    mark_task_run_started_for_workspace(workspace, task)

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.runtime.pipeline.execution_status == "running"
    assert refreshed.runtime.pipeline.current_stage.stage is None
    assert refreshed.runtime.pipeline.current_stage.status == "idle"
    assert refreshed.runtime.execution.active_subagent is None
    assert refreshed.runtime.execution.interruption is None
    _assert_runtime_stage_has_no_removed_fields(refreshed.runtime.pipeline.current_stage)


def test_mark_stage_finished_uses_shared_idle_and_completed_stage_shapes(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish stage")

    workspace = Workspace.from_path(tmp_path)

    mark_stage_started_for_workspace(workspace, task, "implementing")
    task = require_task(tmp_path, task.id)
    report = StageReport(
        task_id=task.id,
        pipeline_state="implementing",
        verdict="pass",
        summary="implemented the change",
    )

    mark_stage_finished_for_workspace(workspace, task, report)

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.runtime.pipeline.current_stage.stage is None
    assert refreshed.runtime.pipeline.current_stage.status == "idle"
    assert "last" + "_stage" not in refreshed.runtime.model_dump()["pipeline"]
    _assert_runtime_stage_has_no_removed_fields(refreshed.runtime.pipeline.current_stage)


def test_task_outcome_failure_diagnostics_are_typed_and_persist_as_object(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Typed outcome diagnostics")

    workspace = Workspace.from_path(tmp_path)

    mark_task_outcome_for_workspace(
        workspace,
        task,
        kind=TaskOutcomeKind.FLAGGED,
        stage="implementing",
        reason_code=OutcomeReasonCode.STAGE_EXCEPTION,
        reason="hook failed",
        retry_count=1,
        retry_limit=2,
        failure_classification="hook_reject",
        failure_diagnostics={"phase": "after_commit", "consecutive_same_hook_rejects": 2},
    )

    refreshed = require_task(tmp_path, task.id)
    diagnostics = refreshed.runtime.pipeline.last_outcome.failure_diagnostics

    assert isinstance(diagnostics, FailureDiagnostics)
    assert diagnostics["phase"] == "after_commit"
    assert diagnostics.get("consecutive_same_hook_rejects") == 2
    assert refreshed.runtime.pipeline.last_outcome.model_dump(mode="json")["failure_diagnostics"] == {
        "phase": "after_commit",
        "consecutive_same_hook_rejects": 2,
    }


def test_mark_subagent_finished_clears_active_subagent_without_completed_runtime_copy(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish subagent")
    ref = _subagent_ref()

    mark_subagent_started(tmp_path, task, ref)

    mark_subagent_finished(tmp_path, task, ref, "SUMMARY: partial output", exit_code=0)

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.runtime.execution.active_subagent is None
    assert "last" + "_subagent" not in refreshed.runtime.model_dump()["execution"]
