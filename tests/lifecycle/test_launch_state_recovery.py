import subprocess
from pathlib import Path

import pytest

from litehive.config.workspace import create_workspace
from litehive.domain.runtime import RuntimeFailedRunRecord, RuntimeStageState
from litehive.lifecycle.journal import SqliteJournal
from litehive.workspace import Workspace
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.system import StubCommitNode
from litehive.lifecycle.orchestration import ExecutionResult, _load_or_initialize, _sync_back, run_task_for_workspace
from litehive.lifecycle.persistence import FailedRunRecord, SqlitePersistence, TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.recovery.execution_recovery import recover_stale_runner_state_for_workspace
from litehive.state.persist import load_state_for_workspace, save_state_for_workspace
from litehive.state.records import create_task_for_workspace, get_task_record_for_workspace, save_task_for_workspace
from litehive.state.store import runtime_store_for_workspace
from litehive.tasks.completed_task_recovery import recover_completed_task_for_workspace
from litehive.tasks.queue import dequeue_next_task
from litehive.domain.common import PipelineState, PipelineStatus, TaskStatus


class _PassEngine:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def run_turn(self, session, prompt, state) -> AgentVerdict:  # type: ignore[no-untyped-def]
        del prompt
        self.calls.append(state.stage)
        session.engine_session_id = f"{state.stage}-{len(self.calls)}"
        return AgentVerdict(outcome="pass")


def _init_workspace_git_repo(root: Path) -> None:
    create_workspace(root)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def _seed_terminal_pipeline_state(
    workspace: Workspace,
    task_id: str,
    *,
    entry_stage: PipelineState,
    stage: PipelineState = PipelineState.FAILED,
) -> None:
    SqlitePersistence(workspace).save(
        TaskState(task_id=task_id, stage=stage, pipeline_mode=PipelineMode.FULL, entry_stage=entry_stage)
    )


def _run_recovered_task(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, task_id: str
) -> tuple[ExecutionResult, list[str], list[str]]:
    queued = dequeue_next_task(workspace)
    assert queued is not None and queued.id == task_id
    monkeypatch.setattr("litehive.lifecycle.orchestration.build_commit_node_for_workspace", lambda workspace: StubCommitNode())
    engine = _PassEngine("stub")
    result = run_task_for_workspace(workspace, workspace.load_config(), queued, engine_factory=lambda _: engine)
    routes = [row["to_stage"] for row in SqliteJournal(workspace).load_transitions(task_id)]
    return result, engine.calls, routes


def _assert_runtime_stage_has_no_removed_fields(stage: RuntimeStageState) -> None:
    for field_name in ("completed_at", "verdict", "summary"):
        assert not hasattr(stage, field_name)


def test_run_task_restarts_recovered_stale_runner_task_from_ready(tmp_path: Path, monkeypatch) -> None:
    _init_workspace_git_repo(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Recover stale runner task")
    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.pipeline.current_stage.stage = PipelineState.IMPLEMENTING
    task.runtime.pipeline.current_stage.status = "running"
    task.runtime.pipeline.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task_for_workspace(workspace, task)
    _seed_terminal_pipeline_state(workspace, task.id, entry_stage=PipelineState.IMPLEMENTING)
    state = load_state_for_workspace(workspace)
    state.active_task_id = task.id
    save_state_for_workspace(workspace, state)

    assert recover_stale_runner_state_for_workspace(workspace) is True
    result, calls, routes = _run_recovered_task(workspace, monkeypatch, task.id)

    assert result.final_stage == "done"
    assert result.task is not None
    _assert_runtime_stage_has_no_removed_fields(result.task.runtime.pipeline.current_stage)
    assert calls == ["implementing", "testing", "accepting"]
    assert routes[:2] == ["worktree_sync", "before_implementing"]


def test_run_task_restarts_recovered_completed_task_from_ready(tmp_path: Path, monkeypatch) -> None:
    _init_workspace_git_repo(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Recover completed task")
    task.status = TaskStatus.DONE
    task.pipeline_status = PipelineStatus.DONE
    save_task_for_workspace(workspace, task)
    _seed_terminal_pipeline_state(workspace, task.id, entry_stage=PipelineState.IMPLEMENTING, stage=PipelineState.DONE)

    recovered = recover_completed_task_for_workspace(workspace, task.id)
    result, calls, routes = _run_recovered_task(workspace, monkeypatch, recovered.id)

    assert result.final_stage == "done"
    assert calls == ["implementing", "testing", "accepting"]
    assert routes[:2] == ["worktree_sync", "before_implementing"]


def test_completed_task_recovery_then_close_reconciles_all_state_layers(tmp_path: Path) -> None:
    """Test that recovery+close operations maintain consistency across all persistence layers."""
    from litehive.tasks.status import close_task_for_workspace

    _init_workspace_git_repo(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    # Create a completed task
    task = create_task_for_workspace(workspace, title="Task for recovery-close test")
    task.status = TaskStatus.DONE
    task.pipeline_status = PipelineStatus.DONE
    save_task_for_workspace(workspace, task)
    _seed_terminal_pipeline_state(workspace, task.id, entry_stage=PipelineState.IMPLEMENTING, stage=PipelineState.DONE)

    # Remove from queue to simulate completed task state (completed tasks are not queued)
    state_before_recovery = load_state_for_workspace(workspace)
    from litehive.tasks.queue import drop_task_from_workspace_state

    drop_task_from_workspace_state(state_before_recovery, task.id)
    save_state_for_workspace(workspace, state_before_recovery)

    # Verify initial completed state
    state_before_recovery = load_state_for_workspace(workspace)
    assert task.id not in state_before_recovery.queue
    assert state_before_recovery.active_task_id != task.id

    # Recover the completed task
    recovered_task = recover_completed_task_for_workspace(workspace, task.id)

    # Verify recovery placed task in queue with correct status
    state_after_recovery = load_state_for_workspace(workspace)
    assert recovered_task.status == "queued"
    assert recovered_task.pipeline_status == "implementing"
    assert task.id in state_after_recovery.queue
    assert state_after_recovery.active_task_id is None

    # Verify SQLite task_state reflects queued status
    store = runtime_store_for_workspace(workspace)
    task_state_after_recovery = store.load_task_state(task.id)
    assert task_state_after_recovery is not None
    assert task_state_after_recovery.status == "queued"
    assert task_state_after_recovery.pipeline_status == "implementing"

    # Close the recovered task as done
    closed_task = close_task_for_workspace(
        workspace,
        task.id,
        outcome="done",
        reason="Task completed successfully",
    )

    # Verify task status is properly closed with outcome "done"
    assert closed_task.status == "done"
    assert closed_task.pipeline_status == "done"
    assert closed_task.close_reason == "done"

    # Verify WorkspaceState.queue no longer contains the task
    state_after_close = load_state_for_workspace(workspace)
    assert task.id not in state_after_close.queue
    assert state_after_close.active_task_id != task.id

    # Verify SQLite task_state reflects done status
    task_state_after_close = store.load_task_state(task.id)
    assert task_state_after_close is not None
    assert task_state_after_close.status == "done"
    assert task_state_after_close.pipeline_status == "done"

    # Verify task record in storage matches in-memory object
    stored_task = get_task_record_for_workspace(workspace, task.id)
    assert stored_task is not None
    assert stored_task.status == "done"
    assert stored_task.pipeline_status == "done"
    assert stored_task.close_reason == "done"

    # Ensure task is never reported as queued/backlog in any persistence layer
    final_state = load_state_for_workspace(workspace)
    final_task_state = store.load_task_state(task.id)
    final_task_record = get_task_record_for_workspace(workspace, task.id)
    assert final_task_record is not None
    assert final_task_state is not None

    # Task should be in done/done state, never queued/backlog
    assert final_task_record.status == "done"
    assert final_task_record.pipeline_status == "done"
    assert final_task_state.status == "done"
    assert final_task_state.pipeline_status == "done"
    assert task.id not in final_state.queue


def test_runtime_failed_run_projection_does_not_seed_fresh_lifecycle_state(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Ignore stale runtime failed-run projection")
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    task.runtime.pipeline.failed_run_history["implementing:stale"] = RuntimeFailedRunRecord(
        stage=PipelineState.IMPLEMENTING,
        failure_shape="stale",
        count=99,
    )
    save_task_for_workspace(workspace, task)

    state = _load_or_initialize(task.id, workspace, SqlitePersistence(workspace))

    assert state.entry_stage == "implementing"
    assert state.failed_run_history == {}


def test_lifecycle_projection_overwrites_stale_runtime_failed_run_state(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Overwrite stale runtime projection")
    task.runtime.pipeline.failed_run_history["implementing:stale"] = RuntimeFailedRunRecord(
        stage=PipelineState.IMPLEMENTING,
        failure_shape="stale",
        count=99,
    )
    save_task_for_workspace(workspace, task)
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    state.failed_run_history["implementing:current"] = FailedRunRecord(
        stage=PipelineState.IMPLEMENTING,
        failure_shape="current",
        count=1,
    )

    refreshed = _sync_back(state, workspace)

    assert refreshed is not None
    assert set(refreshed.runtime.pipeline.failed_run_history) == {"implementing:current"}
    assert refreshed.runtime.pipeline.failed_run_history["implementing:current"].count == 1
