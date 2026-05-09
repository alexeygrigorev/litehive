"""Tests for flag_count lifetime counter, flag threshold handling, and requeue --force."""

import argparse
from pathlib import Path

import pytest

from litehive.config.workspace import create_workspace
from litehive.lifecycle.persistence import SqlitePersistence, TaskNotFound
from litehive.workspace import Workspace
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.tasks.runtime import TaskRuntimeTransitions
from litehive.tasks.status import TaskStatusService

from tests.support.helpers import _cmd_requeue_task
from litehive.domain.common import PipelineState, PipelineStatus, TaskStatus


def _flag_task(workspace: Workspace, task_id: str) -> None:
    """Set a task to flagged and persist via finish_task_run_transition."""
    root = workspace.root
    task = WorkspaceTasks(Workspace.from_path(root)).get(task_id)
    assert task is not None
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).finish_run_transition(task, "flagged")


def test_flag_count_increments_on_each_flagged_transition(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Flaky task")

    _flag_task(workspace, task.id)
    updated = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert updated is not None
    assert updated.flag_count == 1

    # Requeue and flag again
    TaskStatusService(workspace).requeue(task.id, force=True)
    _flag_task(workspace, task.id)
    updated = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert updated is not None
    assert updated.flag_count == 2


def test_auto_defer_after_three_flags(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Repeatedly failing task")

    # Flag 1
    _flag_task(workspace, task.id)
    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    assert t.status == "flagged"
    assert t.flag_count == 1

    # Flag 2
    TaskStatusService(workspace).requeue(task.id, force=True)
    _flag_task(workspace, task.id)
    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    assert t.status == "flagged"
    assert t.flag_count == 2

    # Flag 3 -> manual-review flag reason
    TaskStatusService(workspace).requeue(task.id, force=True)
    _flag_task(workspace, task.id)
    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    assert t.status == "flagged"
    assert t.flag_count == 3
    assert t.flag_reason == "flagged 3 times - needs human review"

    # Task should be out of the queue
    state = WorkspaceStateRepository(Workspace.from_path(tmp_path)).load()
    assert task.id not in state.queue


def test_requeue_blocked_without_force_after_three_flags(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Triple-flagged task")

    # Flag 3 times to reach the threshold
    for i in range(3):
        t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
        assert t is not None
        t.status = TaskStatus.FLAGGED
        t.pipeline_status = PipelineStatus.IMPLEMENTING
        t.flag_count = i  # simulate prior increments
        WorkspaceTasks(Workspace.from_path(tmp_path)).save(t)
        TaskRuntimeTransitions(workspace, WorkspaceTasks(workspace)).finish_run_transition(t, "flagged")

    # Now try to requeue without --force
    with pytest.raises(ValueError, match="flagged 3 times.*--force"):
        TaskStatusService(workspace).requeue(task.id)


def test_requeue_with_force_succeeds_after_three_flags(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Triple-flagged but forced")

    # Set flag_count to 3 and status to flagged (simulating threshold handling)
    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    t.status = TaskStatus.FLAGGED
    t.flag_reason = "flagged 3 times - needs human review"
    t.flag_count = 3
    t.pipeline_status = PipelineStatus.IMPLEMENTING
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(t)

    # Requeue with --force should work
    result = TaskStatusService(workspace).requeue(task.id, force=True)
    assert result.status == "queued"
    assert result.flag_count == 3  # flag_count is NOT reset


def test_flag_count_not_reset_by_requeue(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Counter survives requeue")

    # Flag once
    _flag_task(workspace, task.id)
    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    assert t.flag_count == 1

    # Requeue
    TaskStatusService(workspace).requeue(task.id)
    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    assert t.flag_count == 1  # not reset
    assert t.status == "queued"


def test_requeue_task_resets_sticky_pipeline_failure_state(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Requeue clears failed pipeline state")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.IMPLEMENTING
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(task)

    persistence = SqlitePersistence(workspace)
    failed_state = persistence.initialize(task.id)
    failed_state.stage = PipelineState.FAILED
    persistence.save(failed_state)

    TaskStatusService(workspace).requeue(task.id)

    with pytest.raises(TaskNotFound):
        persistence.load(task.id)


def test_cli_requeue_warns_and_fails_without_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="CLI force check")

    # Set up a flagged task with flag_count >= 3
    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    t.status = TaskStatus.FLAGGED
    t.flag_reason = "flagged 3 times - needs human review"
    t.flag_count = 3
    t.pipeline_status = PipelineStatus.IMPLEMENTING
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(t)

    exit_code = _cmd_requeue_task(argparse.Namespace(workspace=tmp_path, task_id=task.id, front=False, force=False))
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "flagged 3 times" in output


def test_cli_requeue_succeeds_with_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="CLI force requeue")

    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    t.status = TaskStatus.FLAGGED
    t.flag_reason = "flagged 3 times - needs human review"
    t.flag_count = 3
    t.pipeline_status = PipelineStatus.IMPLEMENTING
    WorkspaceTasks(Workspace.from_path(tmp_path)).save(t)

    exit_code = _cmd_requeue_task(argparse.Namespace(workspace=tmp_path, task_id=task.id, front=True, force=True))
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status: queued" in output

    requeued = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert requeued is not None
    assert requeued.flag_count == 3  # NOT reset


def test_new_task_has_flag_count_zero(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = WorkspaceTasks(Workspace.from_path(tmp_path)).create( title="Fresh task")
    assert task.flag_count == 0
    t = WorkspaceTasks(Workspace.from_path(tmp_path)).get(task.id)
    assert t is not None
    assert t.flag_count == 0
