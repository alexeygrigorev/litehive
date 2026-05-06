from pathlib import Path

import pytest
from typer.testing import CliRunner

from litehive.cli.app import app as cli_app
from litehive.config.workspace import ensure_workspace
from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.reports import TaskActivityEntry
from litehive.domain.runtime import RuntimeInterruptionState
from litehive.lifecycle.journal import SqliteJournal
from litehive.workspace import Workspace
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.system import StubCommitNode
from litehive.lifecycle.orchestration import run_task
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, list_tasks, save_task
from litehive.tasks.activity_rendering import append_activity_entry
from litehive.tasks.queue import dequeue_next_task, restore_missing_queued_tasks
from litehive.tasks.status import requeue_task_for_workspace, resume_task, resume_task_for_workspace, stop_current_task
from litehive.worktree.inspection import inspect_dirty_worktree_gate


def _set_running_task(task, *, stage: str = "implementing") -> None:
    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus(stage)
    task.runtime.pipeline.execution_status = "running"
    task.runtime.pipeline.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.pipeline.current_stage.stage = stage
    task.runtime.pipeline.current_stage.status = "running"
    task.runtime.pipeline.current_stage.started_at = "2026-04-12T10:00:00Z"


class _PassEngine:
    def __init__(self, name: str) -> None:
        self.name = name

    def run_turn(self, session, prompt, state) -> AgentVerdict:  # type: ignore[no-untyped-def]
        return AgentVerdict(outcome="pass")


def test_stop_current_task_marks_active_work_as_parked(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Stop me later",
        acceptance_criteria=["resume from current stage"],
    )
    _set_running_task(task)
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    summary = stop_current_task(tmp_path)

    assert summary.task.status == "parked"
    assert summary.task.pipeline_status == "implementing"
    assert summary.task.runtime.pipeline.execution_status == "idle"
    assert summary.task.runtime.pipeline.run_started_at is None
    assert summary.task.runtime.execution.active_subagent is None

    # Parked tasks have minimal interruption metadata for resume functionality
    assert summary.task.runtime.execution.interruption is not None
    assert summary.task.runtime.execution.interruption.resume_stage == "implementing"
    assert summary.task.runtime.execution.interruption.reason == "Task parked via CLI command from implementing stage"
    # No longer using the magic string "Task stopped via CLI"

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "parked"
    assert load_state(tmp_path).active_task_id is None
    assert task.id not in load_state(tmp_path).queue


def test_restore_missing_queued_tasks_skips_parked_and_restores_interrupted(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    interrupted = create_task(
        tmp_path,
        title="Interrupted work",
        acceptance_criteria=["resume interrupted work"],
    )
    interrupted.status = TaskStatus.INTERRUPTED
    interrupted.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task(tmp_path, interrupted)

    parked = create_task(
        tmp_path,
        title="Parked work",
        acceptance_criteria=["resume parked work explicitly"],
    )
    parked.status = TaskStatus.PARKED
    parked.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task(tmp_path, parked)

    backlog = create_task(
        tmp_path,
        title="Dormant backlog work",
        acceptance_criteria=["stay off the live queue until explicitly queued"],
    )
    backlog.status = TaskStatus.QUEUED
    backlog.pipeline_status = PipelineStatus.BACKLOG
    save_task(tmp_path, backlog)

    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    tasks_by_id = {task.id: task for task in list_tasks(tmp_path)}
    restored = restore_missing_queued_tasks(state, tasks_by_id)

    assert restored == [interrupted.id]
    assert state.queue == [interrupted.id]
    assert parked.id not in state.queue
    assert backlog.id not in state.queue


@pytest.mark.parametrize("execution_status", ["interrupted", "idle"])
def test_resume_task_allows_stranded_in_progress_task(tmp_path: Path, execution_status: str) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume stranded task")
    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus.GROOMING
    task.runtime.pipeline.execution_status = execution_status
    task.runtime.pipeline.current_stage.stage = "grooming"
    task.runtime.pipeline.current_stage.status = execution_status
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [item for item in state.queue if item != task.id]
    save_state(tmp_path, state)

    resumed = resume_task(tmp_path, task.id, front=True)

    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.pipeline.execution_status == "idle"
    assert resumed.runtime.pipeline.current_stage.stage == "grooming"
    assert resumed.runtime.pipeline.current_stage.status == "idle"
    assert load_state(tmp_path).queue[0] == task.id


def test_resume_and_requeue_accept_injected_workspace(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    parked = create_task(tmp_path, title="Resume through workspace")
    parked.status = TaskStatus.PARKED
    parked.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task(tmp_path, parked)
    flagged = create_task(tmp_path, title="Requeue through workspace")
    flagged.status = TaskStatus.FLAGGED
    flagged.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task(tmp_path, flagged)

    resumed = resume_task_for_workspace(workspace, parked.id, front=True)
    requeued = requeue_task_for_workspace(workspace, flagged.id, force=True)

    assert resumed.status == "queued"
    assert resumed.pipeline_status == "implementing"
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "implementing"


def test_dirty_worktree_gate_only_auto_attributes_interrupted_tasks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Dirty ownership",
        acceptance_criteria=["allow resume with owned dirty paths"],
    )
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="pass",
            message="Implemented change.",
            files_changed=["src/app.py"],
        ),
    )

    monkeypatch.setattr("litehive.worktree.inspection.is_git_repo", lambda root: True)

    def _status_porcelain(path: Path) -> list[str]:
        if Path(path).resolve() == tmp_path.resolve():
            return [" M src/app.py"]
        return []

    monkeypatch.setattr("litehive.worktree.inspection.status_porcelain", _status_porcelain)

    task.status = TaskStatus.INTERRUPTED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    save_task(tmp_path, task)
    interrupted_report = inspect_dirty_worktree_gate(Workspace.from_path(tmp_path))

    assert interrupted_report.blocks_pool is False
    assert interrupted_report.findings[0].ownership == "task-owned"
    assert interrupted_report.findings[0].task_id == task.id


def test_restarted_execution_enters_saved_resumable_stage(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Restart saved stage",
        acceptance_criteria=["resume in testing without replaying earlier stages"],
    )
    task.status = TaskStatus.PARKED
    task.pipeline_status = PipelineStatus.TESTING
    task.runtime.pipeline.execution_status = "paused"
    task.runtime.pipeline.current_stage.stage = "testing"
    task.runtime.pipeline.current_stage.status = "paused"
    save_task(tmp_path, task)

    resumed = resume_task(tmp_path, task.id, front=True)
    assert resumed.runtime.pipeline.current_stage.stage == "testing"
    assert resumed.runtime.pipeline.current_stage.status == "idle"

    monkeypatch.setattr(
        "litehive.lifecycle.orchestration.build_commit_node",
        lambda root: StubCommitNode(),
    )

    queued = dequeue_next_task(Workspace.from_path(tmp_path))
    assert queued is not None

    result = run_task(
        tmp_path,
        queued,
        engine_factory=lambda name: _PassEngine(name),
    )
    routed_stages = [row["to_stage"] for row in SqliteJournal(Workspace.from_path(tmp_path)).load_transitions(task.id)]

    assert result.final_stage == "done"
    assert routed_stages[:2] == ["worktree_sync", "before_testing"]
    assert "before_grooming" not in routed_stages
    assert "before_implementing" not in routed_stages


def test_resume_task_recovers_preserved_stage_when_pipeline_status_degraded(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Resume preserved stage",
        acceptance_criteria=["resume from testing after stale state cleanup"],
    )
    task.status = TaskStatus.PARKED
    task.pipeline_status = PipelineStatus.BACKLOG
    task.runtime.pipeline.execution_status = "paused"
    task.runtime.pipeline.current_stage.stage = "testing"
    task.runtime.pipeline.current_stage.status = "paused"
    task.runtime.execution.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="Preserve resumable stage",
        summary="Resume from testing.",
    )
    save_task(tmp_path, task)

    resumed = resume_task(tmp_path, task.id, front=True)

    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.pipeline.execution_status == "idle"
    assert resumed.runtime.pipeline.current_stage.stage == "testing"
    assert resumed.runtime.pipeline.current_stage.status == "idle"
    assert load_state(tmp_path).queue[0] == task.id


def test_resume_task_canonicalizes_stranded_in_progress_with_degraded_pipeline_status(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Resume stranded degraded task",
        acceptance_criteria=["resume from testing"],
    )
    task.status = TaskStatus.IN_PROGRESS
    task.pipeline_status = PipelineStatus.BACKLOG
    task.runtime.pipeline.execution_status = "idle"
    task.runtime.pipeline.current_stage.stage = "testing"
    task.runtime.pipeline.current_stage.status = "idle"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    resumed = resume_task(tmp_path, task.id, front=True)

    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.pipeline.execution_status == "idle"
    assert resumed.runtime.pipeline.current_stage.stage == "testing"
    assert resumed.runtime.pipeline.current_stage.status == "idle"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task.id


def test_queue_resume_and_requeue_keep_parked_semantics_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.delenv("LITEHIVE_AGENT_ROLE", raising=False)
    runner = CliRunner()

    task = create_task(
        tmp_path,
        title="Queued later",
        acceptance_criteria=["resume from testing", "requeue from implementing"],
    )
    task.status = TaskStatus.PARKED
    task.pipeline_status = PipelineStatus.TESTING
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    queue_result = runner.invoke(
        cli_app,
        ["queue", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )
    assert queue_result.exit_code == 0, queue_result.output
    assert f"resume 1. {task.id} [parked/testing]" in queue_result.output

    resume_result = runner.invoke(
        cli_app,
        ["queue", "resume", task.id, "--workspace", str(tmp_path)],
        standalone_mode=False,
    )
    assert resume_result.exit_code == 0, resume_result.output
    assert "status: queued" in resume_result.output
    assert "pipeline_stage: testing" in resume_result.output

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"

    # Use model_copy(update=...) to bypass pyrefly's pydantic
    # validate_assignment narrowing (the equality assertions above collapse
    # the field types to literals, so direct assignment then looks bad).
    refreshed = refreshed.model_copy(update={"status": TaskStatus.PARKED, "pipeline_status": PipelineStatus.TESTING})
    save_task(tmp_path, refreshed)
    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    requeue_result = runner.invoke(
        cli_app,
        ["queue", "requeue", task.id, "--workspace", str(tmp_path)],
        standalone_mode=False,
    )
    assert requeue_result.exit_code == 0, requeue_result.output
    assert "status: queued" in requeue_result.output
    assert "pipeline_stage: implementing" in requeue_result.output

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
