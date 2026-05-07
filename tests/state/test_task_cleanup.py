import logging
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

from litehive.config.workspace import create_workspace
from litehive.state.records import create_task, discard_created_task_for_workspace
from litehive.tasks.paths import task_dir
from litehive.workspace import Workspace


def test_discard_created_task_missing_dir_no_error(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Ephemeral")
    td = task_dir(tmp_path, task)
    shutil.rmtree(td)
    assert not td.exists()

    discard_created_task_for_workspace(Workspace.from_path(tmp_path), task.id)


def test_discard_created_task_existing_dir_removed(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Removable")
    td = task_dir(tmp_path, task)
    assert td.exists()

    discard_created_task_for_workspace(Workspace.from_path(tmp_path), task.id)

    assert not td.exists()


def test_discard_created_task_logs_target_and_raises_on_cleanup_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Cleanup failure")
    td = task_dir(tmp_path, task)

    with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.INFO, logger="litehive.state.records"):
            with pytest.raises(OSError, match="failed to delete task directory .*permission denied"):
                discard_created_task_for_workspace(Workspace.from_path(tmp_path), task.id)

    assert f"Deleting task directory {td}" in caplog.text
    assert f"Failed to delete task directory {td}" in caplog.text
