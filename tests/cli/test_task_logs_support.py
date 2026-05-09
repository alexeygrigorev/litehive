from pathlib import Path

from litehive.cli.task_logs_support import TaskLogsPresenter
from litehive.config.workspace import create_workspace
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace


def test_load_task_with_runtime_tolerates_unrelated_missing_runtime_rows(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Journal task", auto_commit=False)

    missing_dir = tmp_path / ".litehive" / "tasks" / "T-0002-missing-runtime"
    missing_dir.mkdir(parents=True)

    loaded = TaskLogsPresenter(workspace).load_task_with_runtime(task.id)

    assert loaded is not None
    assert loaded.id == task.id
