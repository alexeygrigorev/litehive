import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task, save_task
from litehive.domain.common import TaskStatus
from litehive.worktree.cleanup import remove_cleanable_worktrees_for_workspace
from litehive.worktree.execution_root import resolve_task_execution_root_for_workspace
from litehive.worktree.paths import ensure_worktree_venv_link, task_worktree_path
from litehive.workspace import Workspace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_ok(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc.stdout.strip()


def _configure_repo(path: Path) -> None:
    _git_ok(path, "config", "user.email", "test@example.com")
    _git_ok(path, "config", "user.name", "Test User")


def test_resolve_task_execution_root_links_worktree_venv_to_workspace_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    (workspace / ".venv").mkdir()
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Resolve execution root")

    worktree = resolve_task_execution_root_for_workspace(Workspace.from_path(workspace), task)

    assert worktree.joinpath(".venv").is_symlink()
    assert worktree.joinpath(".venv").resolve() == workspace.joinpath(".venv").resolve()


def test_resolve_task_execution_root_accepts_injected_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Resolve execution root from workspace")

    worktree = resolve_task_execution_root_for_workspace(Workspace.from_path(workspace), task)

    assert worktree == task_worktree_path(workspace, task)
    assert worktree.exists()


def test_ensure_worktree_venv_link_logs_target_and_raises_on_cleanup_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".venv").mkdir()
    worktree = workspace / "task-worktree"
    (worktree / ".venv").mkdir(parents=True)

    with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.INFO, logger="litehive.worktree"):
            with pytest.raises(OSError, match="failed to delete worktree venv directory .*permission denied"):
                ensure_worktree_venv_link(workspace, worktree)

    assert f"Deleting worktree venv directory {worktree / '.venv'}" in caplog.text
    assert f"Failed to delete worktree venv directory {worktree / '.venv'}" in caplog.text


def test_resolve_task_execution_root_logs_target_and_raises_on_worktree_cleanup_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Cleanup stale worktree")
    stale_worktree = task_worktree_path(workspace, task)
    stale_worktree.mkdir(parents=True)

    with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.INFO, logger="litehive.worktree"):
            with pytest.raises(OSError, match="failed to delete task worktree directory .*permission denied"):
                resolve_task_execution_root_for_workspace(Workspace.from_path(workspace), task)

    assert f"Deleting task worktree directory {stale_worktree}" in caplog.text
    assert f"Failed to delete task worktree directory {stale_worktree}" in caplog.text


def test_remove_cleanable_worktrees_includes_closed_tasks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    task = create_task(workspace, title="Closed worktree cleanup")
    worktree = task_worktree_path(workspace, task)
    worktree.mkdir(parents=True)
    task.status = TaskStatus.CLOSED
    task.close_reason = "deferred"
    task.runtime.pipeline.git.worktree_path = str(worktree)
    save_task(workspace, task)

    result = remove_cleanable_worktrees_for_workspace(Workspace.from_path(workspace), dry_run=True)

    assert [item.task_id for item in result["candidates"]] == [task.id]
