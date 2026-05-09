from pathlib import Path

from heru.types import SubagentRef

from litehive.config.workspace import create_workspace
from litehive.domain.common import PipelineStatus, TaskExecutionStatus, TaskStatus
from litehive.domain.outcomes import OutcomeReasonCode, TaskOutcomeKind
from litehive.domain.failure_diagnostics import FailureDiagnostics
from litehive.domain.reports import StageReport
from litehive.domain.runtime import RuntimeInterruptionState, RuntimeStageState
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.tasks.runtime import TaskRuntimeTransitions
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
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Reset runtime")

    workspace = Workspace.from_path(tmp_path)

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).start_stage(task, "implementing")
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).require(task.id)
    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).mark_subagent_started(task, _subagent_ref())

    task = WorkspaceTasks(Workspace.from_path(tmp_path)).require(task.id)
    task.runtime.execution.interruption = RuntimeInterruptionState(source="runner", reason="stale state")
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(task)

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).start_run(task)

    refreshed = WorkspaceTasks(Workspace.from_path(tmp_path)).require(task.id)
    assert refreshed.runtime.pipeline.execution_status == "running"
    assert refreshed.runtime.pipeline.current_stage.stage is None
    assert refreshed.runtime.pipeline.current_stage.status == "idle"
    assert refreshed.runtime.execution.active_subagent is None
    assert refreshed.runtime.execution.interruption is None
    _assert_runtime_stage_has_no_removed_fields(refreshed.runtime.pipeline.current_stage)


def test_mark_task_run_finished_clears_run_activity_and_persists_status(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Finish runtime")
    ref = _subagent_ref()

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).start_run(task)
    task = WorkspaceTasks(workspace).require(task.id)
    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).start_stage(task, "implementing")
    task = WorkspaceTasks(workspace).require(task.id)
    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).mark_subagent_started(task, ref)
    task = WorkspaceTasks(workspace).require(task.id)

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).finish_run(task, TaskExecutionStatus.INTERRUPTED)

    refreshed = WorkspaceTasks(workspace).require(task.id)
    assert refreshed.runtime.pipeline.execution_status == TaskExecutionStatus.INTERRUPTED
    assert refreshed.runtime.pipeline.run_started_at is None
    assert refreshed.runtime.execution.active_subagent is None


def test_mark_stage_finished_uses_shared_idle_and_completed_stage_shapes(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Finish stage")

    workspace = Workspace.from_path(tmp_path)

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).start_stage(task, "implementing")
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).require(task.id)
    report = StageReport(
        task_id=task.id,
        pipeline_state="implementing",
        verdict="pass",
        summary="implemented the change",
    )

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).finish_stage(task, report)

    refreshed = WorkspaceTasks(Workspace.from_path(tmp_path)).require(task.id)
    assert refreshed.runtime.pipeline.current_stage.stage is None
    assert refreshed.runtime.pipeline.current_stage.status == "idle"
    assert "last" + "_stage" not in refreshed.runtime.model_dump()["pipeline"]
    _assert_runtime_stage_has_no_removed_fields(refreshed.runtime.pipeline.current_stage)


def test_mark_subagent_progress_updates_pid_trace_and_stage_timestamp(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Subagent progress")
    ref = _subagent_ref()

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).start_stage(task, "implementing")
    task = WorkspaceTasks(workspace).require(task.id)
    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).mark_subagent_started(task, ref)
    task = WorkspaceTasks(workspace).require(task.id)

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).mark_subagent_progress(
        task,
        pid=1234,
        transcript="SUMMARY: working on the fix\n",
    )

    refreshed = WorkspaceTasks(workspace).require(task.id)
    active = refreshed.runtime.execution.active_subagent
    assert active is not None
    assert active.pid == 1234
    assert active.execution_trace_snippet == "working on the fix"
    assert refreshed.runtime.pipeline.current_stage.stage == "implementing"
    assert refreshed.runtime.pipeline.current_stage.updated_at is not None


def test_task_outcome_failure_diagnostics_are_typed_and_persist_as_object(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Typed outcome diagnostics")

    workspace = Workspace.from_path(tmp_path)

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).record_outcome(
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

    refreshed = WorkspaceTasks(Workspace.from_path(tmp_path)).require(task.id)
    diagnostics = refreshed.runtime.pipeline.last_outcome.failure_diagnostics

    assert isinstance(diagnostics, FailureDiagnostics)
    assert diagnostics["phase"] == "after_commit"
    assert diagnostics.get("consecutive_same_hook_rejects") == 2
    assert refreshed.runtime.pipeline.last_outcome.model_dump(mode="json")["failure_diagnostics"] == {
        "phase": "after_commit",
        "consecutive_same_hook_rejects": 2,
    }


def test_mark_engine_switch_persists_last_engine_switch(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Engine switch")

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).switch_engine(
        task,
        stage="implementing",
        from_engine="codex",
        to_engine="claude",
        reason="quota pressure",
    )

    refreshed = WorkspaceTasks(workspace).require(task.id)
    engine_switch = refreshed.runtime.execution.last_engine_switch
    assert engine_switch is not None
    assert engine_switch.stage == "implementing"
    assert engine_switch.from_engine == "codex"
    assert engine_switch.to_engine == "claude"
    assert engine_switch.reason == "quota pressure"


def test_mark_subagent_finished_clears_active_subagent_without_completed_runtime_copy(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Finish subagent")
    ref = _subagent_ref()

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).mark_subagent_started(task, ref)

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).mark_subagent_finished(
        task,
        ref,
        "SUMMARY: partial output",
        exit_code=0,
    )

    refreshed = WorkspaceTasks(Workspace.from_path(tmp_path)).require(task.id)
    assert refreshed.runtime.execution.active_subagent is None
    assert "last" + "_subagent" not in refreshed.runtime.model_dump()["execution"]


def test_finish_task_run_transition_requeues_paused_task_and_clears_active(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Pause and requeue")
    task.status = TaskStatus.QUEUED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    WorkspaceTasks(workspace).save(task)
    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = task.id
    state.queue = []
    WorkspaceStateRepository(workspace).save_without_runner_guard(state)

    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).finish_run_transition(task, TaskExecutionStatus.PAUSED)

    refreshed = WorkspaceTasks(workspace).require(task.id)
    refreshed_state = WorkspaceStateRepository(workspace).load()
    assert refreshed.runtime.pipeline.execution_status == TaskExecutionStatus.PAUSED
    assert refreshed.runtime.execution.active_subagent is None
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue == [task.id]
