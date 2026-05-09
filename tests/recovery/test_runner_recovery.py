import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from litehive.agents.session_store import SubagentArtifactPayload, subagent_artifacts
from litehive.config.workspace import create_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.runtime import RuntimeStageState, RuntimeSubagentState
from litehive.recovery.execution_recovery import RunnerRecoveryService, prepare_interrupted_task
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace
from litehive.domain.common import PipelineStatus, TaskStatus


def _seed_running_task(tmp_path: Path, *, stage: str, active: bool) -> tuple[Workspace, str, str]:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title=f"{stage} recovery")
    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus(stage)
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.pipeline.current_stage.stage = stage
    task.runtime.pipeline.current_stage.status = "running"
    task.runtime.pipeline.current_stage.started_at = "2026-04-12T10:00:00Z"
    WorkspaceTasks(workspace).save(task)

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = task.id if active else None
    WorkspaceStateRepository(workspace).save(state)
    return workspace, task.id, stage


def _rewrite_runtime_payload_as_legacy_flat(root: Path, task_id: str) -> None:
    with connect_workspace_db(root) as connection:
        row = connection.execute(
            "SELECT payload, updated_at FROM task_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        runtime = payload["runtime"]
        flat_runtime = {
            **runtime.get("pipeline", {}),
            **runtime.get("execution", {}),
        }
        payload["runtime"] = flat_runtime
        connection.execute(
            "UPDATE task_state SET payload = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(payload, sort_keys=True), row["updated_at"], task_id),
        )
        connection.commit()


def _task_state_runtime_payload(root: Path, task_id: str) -> dict:
    with connect_workspace_db(root) as connection:
        row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row["payload"])["runtime"]


def _assert_runtime_stage_has_no_removed_fields(stage: RuntimeStageState) -> None:
    for field_name in ("completed_at", "verdict", "summary"):
        assert not hasattr(stage, field_name)


@pytest.mark.parametrize(
    ("stage", "active"),
    [("implementing", True), ("commit_to_git", True), ("implementing", False)],
)
def test_recover_stale_runner_state_requeues_running_task(tmp_path: Path, stage: str, active: bool) -> None:
    workspace, task_id, expected_stage = _seed_running_task(tmp_path, stage=stage, active=active)

    assert RunnerRecoveryService(workspace).recover_stale_runner_state() is True

    refreshed = WorkspaceTasks(workspace).get(task_id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == expected_stage
    assert refreshed.runtime.pipeline.execution_status == "idle"
    assert refreshed.runtime.pipeline.current_stage.stage == expected_stage
    assert refreshed.runtime.pipeline.current_stage.status == "idle"
    assert refreshed.runtime.execution.interruption is not None
    assert refreshed.runtime.execution.interruption.stage == expected_stage
    assert refreshed.runtime.execution.interruption.resume_stage == expected_stage
    _assert_runtime_stage_has_no_removed_fields(refreshed.runtime.pipeline.current_stage)

    refreshed_state = WorkspaceStateRepository(workspace).load()
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task_id


def test_recover_stale_runner_state_does_not_use_flat_runtime_payload(tmp_path: Path) -> None:
    workspace, task_id, _ = _seed_running_task(tmp_path, stage="implementing", active=True)
    _rewrite_runtime_payload_as_legacy_flat(tmp_path, task_id)

    assert RunnerRecoveryService(workspace).recover_stale_runner_state() is False

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkspaceTasks(workspace).get(task_id)

    raw_payload = _task_state_runtime_payload(tmp_path, task_id)
    assert "execution_status" in raw_payload
    assert "pipeline" not in raw_payload

    refreshed_state = WorkspaceStateRepository(workspace).load()
    assert refreshed_state.active_task_id == task_id


def test_recover_stale_runner_state_preserves_runtime_stage_when_pipeline_status_degraded(
    tmp_path: Path,
) -> None:
    workspace, task_id, _ = _seed_running_task(tmp_path, stage="testing", active=True)
    task = WorkspaceTasks(workspace).get(task_id)
    assert task is not None
    task.pipeline_status = PipelineStatus.BACKLOG
    WorkspaceTasks(workspace).save(task)

    assert RunnerRecoveryService(workspace).recover_stale_runner_state() is True

    refreshed = WorkspaceTasks(workspace).get(task_id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.pipeline.execution_status == "idle"
    assert refreshed.runtime.pipeline.current_stage.stage == "testing"
    assert refreshed.runtime.pipeline.current_stage.status == "idle"
    assert refreshed.runtime.execution.interruption is not None
    assert refreshed.runtime.execution.interruption.resume_stage == "testing"

    refreshed_state = WorkspaceStateRepository(workspace).load()
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task_id


def test_recover_stale_runner_state_canonicalizes_nonrunning_stranded_task(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(
        title="Stranded non-running task",
        acceptance_criteria=["resume from testing"],
    )
    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus.BACKLOG
    task.runtime.pipeline.execution_status = "idle"
    task.runtime.pipeline.current_stage.stage = "testing"
    task.runtime.pipeline.current_stage.status = "idle"
    WorkspaceTasks(workspace).save(task)

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = None
    state.queue = []
    WorkspaceStateRepository(workspace).save(state)

    assert RunnerRecoveryService(workspace).recover_stale_runner_state() is True

    refreshed = WorkspaceTasks(workspace).get(task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.pipeline.execution_status == "idle"
    assert refreshed.runtime.pipeline.current_stage.stage == "testing"
    assert refreshed.runtime.pipeline.current_stage.status == "idle"

    refreshed_state = WorkspaceStateRepository(workspace).load()
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task.id


def test_recover_stale_runner_state_canonicalizes_queued_interrupted_marker(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(
        title="Queued interrupted task",
        acceptance_criteria=["resume from grooming"],
    )
    task.status = TaskStatus.QUEUED
    task.pipeline_status = PipelineStatus.GROOMING
    task.runtime.pipeline.execution_status = "interrupted"
    task.runtime.pipeline.current_stage.stage = "grooming"
    task.runtime.pipeline.current_stage.status = "running"
    WorkspaceTasks(workspace).save(task)

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = None
    state.queue = [task.id]
    WorkspaceStateRepository(workspace).save(state)

    assert RunnerRecoveryService(workspace).recover_stale_runner_state() is True

    refreshed = WorkspaceTasks(workspace).get(task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "grooming"
    assert refreshed.runtime.pipeline.execution_status == "idle"
    assert refreshed.runtime.pipeline.current_stage.stage == "grooming"
    assert refreshed.runtime.pipeline.current_stage.status == "idle"

    refreshed_state = WorkspaceStateRepository(workspace).load()
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue == [task.id]


def test_recover_stale_runner_state_clears_non_running_active_task_id(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Flagged but not running")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.FLAGGED
    task.runtime.pipeline.execution_status = "idle"
    WorkspaceTasks(workspace).save(task)

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = task.id
    WorkspaceStateRepository(workspace).save(state)

    assert RunnerRecoveryService(workspace).recover_stale_runner_state() is True
    assert WorkspaceTasks(workspace).get(task.id).runtime.pipeline.execution_status == "idle"  # type: ignore[union-attr]
    assert WorkspaceStateRepository(workspace).load().active_task_id is None


def test_prepare_interrupted_task_writes_resume_bookkeeping(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Interrupted run")
    subagent_path = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "subagents" / "SA-1234-swe"
    subagent_path.mkdir(parents=True)
    subagent_artifacts(workspace, task.id, "SA-1234").save(
        session=SubagentArtifactPayload({"status": "running"}),
        report=SubagentArtifactPayload({"summary": "finished half the change"}),
    )

    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.pipeline.current_stage.stage = "implementing"
    task.runtime.pipeline.current_stage.status = "running"
    task.runtime.pipeline.current_stage.started_at = "2026-04-12T10:00:00Z"
    task.runtime.execution.active_subagent = RuntimeSubagentState(
        id="SA-1234",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-1234-swe",
        sandboxed=False,
        sandbox_summary="host",
        started_at="2026-04-12T10:00:00Z",
        updated_at="2026-04-12T10:05:00Z",
    )

    prepare_interrupted_task(
        workspace,
        task,
        stage="implementing",
        summary="Interrupted run recovered. Resume from `implementing`.",
        reason="received ctrl-c",
    )

    assert task.runtime.execution.interruption is not None
    assert task.runtime.execution.interruption.reason == "received ctrl-c"
    assert task.runtime.execution.interruption.resume_stage == "implementing"
    assert task.runtime.execution.active_subagent is None
    assert task.runtime.execution.interruption.subagent is not None
    assert task.runtime.execution.interruption.subagent.status == "interrupted"
    _assert_runtime_stage_has_no_removed_fields(task.runtime.pipeline.current_stage)

    session = subagent_artifacts(workspace, task.id, "SA-1234").load_session_record()
    report = subagent_artifacts(workspace, task.id, "SA-1234").load_report()
    assert session.values["status"] == report["status"] == "interrupted"
    assert session.values["resume_stage"] == report["resume_stage"] == "implementing"
    assert session.values["interruption_reason"] == report["interruption_reason"] == "received ctrl-c"
