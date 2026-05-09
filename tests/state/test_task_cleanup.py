import logging
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

from litehive.config.workspace import create_workspace
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace


def test_discard_created_task_missing_dir_no_error(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(title="Ephemeral")
    td = workspace.task_dir(task)
    shutil.rmtree(td)
    assert not td.exists()

    WorkspaceTasks(workspace).discard_created(task.id)


def test_discard_created_task_existing_dir_removed(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(title="Removable")
    td = workspace.task_dir(task)
    assert td.exists()

    WorkspaceTasks(workspace).discard_created(task.id)

    assert not td.exists()


def test_discard_created_task_logs_target_and_raises_on_cleanup_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create(title="Cleanup failure")
    td = workspace.task_dir(task)

    with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.INFO, logger="litehive.state.records"):
            with pytest.raises(OSError, match="failed to delete task directory .*permission denied"):
                WorkspaceTasks(workspace).discard_created(task.id)

    assert f"Deleting task directory {td}" in caplog.text
    assert f"Failed to delete task directory {td}" in caplog.text
