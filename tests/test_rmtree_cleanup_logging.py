"""Tests for rmtree cleanup logging (T-0202)."""

import logging
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from litehive.config import ensure_workspace
from litehive.daemon import prune_run_all_log_dirs
from litehive.tasks import create_task
from litehive.tasks.crud import discard_created_task
from litehive.tasks.paths import task_dir


def test_discard_created_task_missing_dir_no_error(tmp_path: Path) -> None:
    """Discarding a task whose dir is already gone should not raise."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ephemeral")
    td = task_dir(tmp_path, task)
    shutil.rmtree(td)
    assert not td.exists()
    # Should not raise even though the directory is already gone
    discard_created_task(tmp_path, task.id)


def test_discard_created_task_existing_dir_removed(tmp_path: Path) -> None:
    """Discarding a task with an existing dir should remove it."""
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Removable")
    td = task_dir(tmp_path, task)
    assert td.exists()
    discard_created_task(tmp_path, task.id)
    assert not td.exists()


def test_prune_log_dirs_logs_on_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Pruning log dirs that fail to delete should log warnings."""
    log_base = tmp_path / "logs" / "run-all"
    log_base.mkdir(parents=True)
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (log_base / name).mkdir()

    with patch("litehive.daemon.logs.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.WARNING, logger="litehive.daemon.logs"):
            prune_run_all_log_dirs(log_base, keep=1)

    # Two dirs should have been attempted for pruning (keep=1 means remove 2)
    assert caplog.text.count("Failed to prune log dir") == 2
    assert "permission denied" in caplog.text


def test_prune_log_dirs_removes_old(tmp_path: Path) -> None:
    """Old log dirs are actually removed when pruning succeeds."""
    log_base = tmp_path / "logs" / "run-all"
    log_base.mkdir(parents=True)
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (log_base / name).mkdir()

    prune_run_all_log_dirs(log_base, keep=1)

    remaining = sorted(p.name for p in log_base.iterdir())
    assert remaining == ["2026-01-03"]
