import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.git.ops import GitError
from litehive.recovery.launch import LaunchFailure, attempt_launch_recovery, prepare_task_launch
from litehive.state.records import create_task, get_task


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


def test_prepare_task_launch_links_worktree_venv_to_workspace_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    (workspace / ".venv").mkdir()
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Prepare task launch")

    prepare_task_launch(workspace, task)

    refreshed = get_task(workspace, task.id)
    assert refreshed is not None
    worktree = Path(refreshed.runtime.git.worktree_path)
    assert worktree.joinpath(".venv").is_symlink()
    assert worktree.joinpath(".venv").resolve() == workspace.joinpath(".venv").resolve()


def test_attempt_launch_recovery_logs_target_and_raises_on_worktree_cleanup_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover task worktree")
    worktree = tmp_path / "stale-worktree"
    worktree.mkdir()
    task.runtime.git.worktree_path = str(worktree)

    with patch("litehive.recovery.launch.remove_worktree", side_effect=GitError("registered worktree missing")):
        with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
            with caplog.at_level(logging.INFO, logger="litehive.recovery.launch"):
                with pytest.raises(OSError, match="failed to delete stale task worktree directory .*permission denied"):
                    attempt_launch_recovery(
                        tmp_path,
                        task,
                        LaunchFailure(
                            context="worktree_setup_failed",
                            summary="git worktree add failed: stale metadata",
                        ),
                    )

    assert f"Deleting stale task worktree directory {worktree}" in caplog.text
    assert f"Failed to delete stale task worktree directory {worktree}" in caplog.text
