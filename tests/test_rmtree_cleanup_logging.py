"""Tests for logged rmtree cleanup replacing ignore_errors=True."""

import logging
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from litehive.daemon import _prune_run_all_log_dirs
from litehive.tasks import discard_created_task


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    from litehive.config import ensure_workspace

    ensure_workspace(tmp_path)
    return tmp_path


def test_discard_created_task_missing_dir_no_error(workspace: Path) -> None:
    """discard_created_task on a nonexistent task does not raise."""
    # Task ID that doesn't exist — get_task returns None, no rmtree attempted
    discard_created_task(workspace, "T-9999-ghost")


def test_discard_created_task_existing_dir_removed(workspace: Path) -> None:
    """discard_created_task removes an existing task directory."""
    from litehive.tasks import create_task

    task = create_task(workspace, title="throwaway task")
    from litehive.tasks import task_dir as _task_dir

    td = _task_dir(workspace, task)
    assert td.exists()
    discard_created_task(workspace, task.id)
    assert not td.exists()


def test_prune_log_dirs_logs_on_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_prune_run_all_log_dirs logs a warning when rmtree fails."""
    log_base = tmp_path / "logs"
    log_base.mkdir()
    # Create 3 dirs; keep=1 means 2 will be pruned
    for name in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        (log_base / name).mkdir()

    def fail_rmtree(path, *args, **kwargs):
        raise OSError(f"permission denied: {path}")

    with patch.object(shutil, "rmtree", side_effect=fail_rmtree):
        with caplog.at_level(logging.WARNING, logger="litehive.daemon"):
            _prune_run_all_log_dirs(log_base, keep=1)

    assert len(caplog.records) == 2
    assert "2026-01-01" in caplog.text
    assert "2026-01-02" in caplog.text
    assert "permission denied" in caplog.text


def test_prune_log_dirs_removes_old(tmp_path: Path) -> None:
    """_prune_run_all_log_dirs removes dirs beyond the keep threshold."""
    log_base = tmp_path / "logs"
    log_base.mkdir()
    for name in ["a", "b", "c"]:
        (log_base / name).mkdir()

    _prune_run_all_log_dirs(log_base, keep=1)
    remaining = sorted(p.name for p in log_base.iterdir())
    assert remaining == ["c"]
