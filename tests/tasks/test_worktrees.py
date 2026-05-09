import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from litehive.config.workspace import create_workspace
from litehive.state.records import WorkspaceTasks
from litehive.domain.common import TaskStatus
from litehive.worktree.cleanup import WorktreeCleanupService
from litehive.worktree.execution_root import TaskExecutionRootResolver
from litehive.worktree.paths import WorktreePaths
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
    create_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    (workspace / ".venv").mkdir()
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = WorkspaceTasks(Workspace.from_path(workspace)).create( title="Resolve execution root")

    worktree = TaskExecutionRootResolver(Workspace.from_path(workspace)).resolve(task)

    assert worktree.joinpath(".venv").is_symlink()
    assert worktree.joinpath(".venv").resolve() == workspace.joinpath(".venv").resolve()


def test_resolve_task_execution_root_accepts_injected_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    create_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = WorkspaceTasks(Workspace.from_path(workspace)).create( title="Resolve execution root from workspace")

    worktree = TaskExecutionRootResolver(Workspace.from_path(workspace)).resolve(task)

    assert worktree == WorktreePaths(Workspace.from_path(workspace)).task_worktree_path(task)
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
                WorktreePaths(Workspace.from_path(workspace)).ensure_venv_link(worktree)

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
    create_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = WorkspaceTasks(Workspace.from_path(workspace)).create( title="Cleanup stale worktree")
    stale_worktree = WorktreePaths(Workspace.from_path(workspace)).task_worktree_path(task)
    stale_worktree.mkdir(parents=True)

    with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
        with caplog.at_level(logging.INFO, logger="litehive.worktree"):
            with pytest.raises(OSError, match="failed to delete task worktree directory .*permission denied"):
                TaskExecutionRootResolver(Workspace.from_path(workspace)).resolve(task)

    assert f"Deleting task worktree directory {stale_worktree}" in caplog.text
    assert f"Failed to delete task worktree directory {stale_worktree}" in caplog.text


def test_remove_cleanable_worktrees_includes_closed_tasks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    create_workspace(workspace)

    task = WorkspaceTasks(Workspace.from_path(workspace)).create( title="Closed worktree cleanup")
    worktree = WorktreePaths(Workspace.from_path(workspace)).task_worktree_path(task)
    worktree.mkdir(parents=True)
    task.status = TaskStatus.CLOSED
    task.close_reason = "deferred"
    task.runtime.pipeline.git.worktree_path = str(worktree)
    WorkspaceTasks(Workspace.from_path(workspace)).save(task)

    result = WorktreeCleanupService(Workspace.from_path(workspace)).remove_cleanable_worktrees(dry_run=True)

    assert [item.task_id for item in result["candidates"]] == [task.id]
