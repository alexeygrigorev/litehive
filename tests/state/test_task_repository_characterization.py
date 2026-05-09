from pathlib import Path

import pytest

from litehive.config.workspace import create_workspace, render_workspace_gitignore
from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.state.records import (
    WorkspaceTasks,
                        WorkspaceTasks,
)
from litehive.workspace import Workspace


def test_task_repository_shape_covers_create_list_get_require_and_save(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)

    first = WorkspaceTasks(workspace).create( title="First task", goal="initial goal")
    second = WorkspaceTasks(workspace).create( title="Second task")
    listed = WorkspaceTasks(workspace).list()

    assert [task.id for task in listed] == ["T-0001", "T-0002"]
    assert [task.title for task in listed] == ["First task", "Second task"]
    assert WorkspaceTasks(workspace).require(first.id).goal == "initial goal"
    assert WorkspaceTasks(workspace).require(second.id).title == "Second task"

    first.goal = "saved goal"
    WorkspaceTasks(workspace).save(first)

    assert WorkspaceTasks(workspace).require(first.id).goal == "saved goal"
    with pytest.raises(ValueError, match="Task T-9999 not found"):
        WorkspaceTasks(workspace).require("T-9999")


def test_task_repository_shape_covers_runtime_writes_and_gitignore_refresh(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Runtime task")
    gitignore_path = workspace.control_files().gitignore()
    gitignore_path.write_text("stale\n", encoding="utf-8")

    task.status = TaskStatus.DONE
    task.pipeline_status = PipelineStatus.DONE
    WorkspaceTasks(workspace).write_runtime(task)

    refreshed = WorkspaceTasks(workspace).require(task.id)
    assert refreshed.status == TaskStatus.DONE
    assert refreshed.pipeline_status == PipelineStatus.DONE
    assert gitignore_path.read_text(encoding="utf-8") == render_workspace_gitignore()

    refreshed.status = TaskStatus.FLAGGED
    refreshed.pipeline_status = PipelineStatus.FLAGGED
    WorkspaceTasks(workspace).save_runtime(refreshed)

    saved = WorkspaceTasks(workspace).require(task.id)
    assert saved.status == TaskStatus.FLAGGED
    assert saved.pipeline_status == PipelineStatus.FLAGGED


def test_task_repository_shape_covers_discard_created_task(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Discard me")
    task_dir = workspace.task_dir(task)

    assert task_dir.exists()

    WorkspaceTasks(workspace).discard_created(task.id)

    assert WorkspaceTasks(workspace).get(task.id) is None
    assert task_dir.exists() is False
    assert WorkspaceTasks(workspace).list() == []
