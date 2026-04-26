import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.git.ops import GitError
from litehive.recovery.detection import LaunchFailure, TaskLaunchFailure
from litehive.recovery.execution_recovery import attempt_launch_recovery, prepare_task_launch
from litehive.state.records import create_task, get_task
from litehive.worktree import resolve_task_execution_root, task_worktree_path


def _git_ok(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc.stdout.strip()


def _init_workspace_repo(workspace: Path) -> None:
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _git_ok(workspace, "config", "user.email", "test@example.com")
    _git_ok(workspace, "config", "user.name", "Test User")
    ensure_workspace(workspace)
    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    (workspace / ".venv").mkdir()
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")


def test_prepare_task_launch_links_worktree_venv_to_workspace_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_workspace_repo(workspace)
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

    with patch(
        "litehive.recovery.execution_recovery.remove_worktree", side_effect=GitError("registered worktree missing")
    ):
        with patch("litehive.fs_cleanup.shutil.rmtree", side_effect=OSError("permission denied")):
            with caplog.at_level(logging.INFO, logger="litehive.recovery.execution_recovery"):
                with pytest.raises(OSError, match="failed to delete stale task worktree directory .*permission denied"):
                    attempt_launch_recovery(
                        tmp_path,
                        task,
                        LaunchFailure(
                            context="worktree_setup_failed", summary="git worktree add failed: stale metadata"
                        ),
                    )

    assert f"Deleting stale task worktree directory {worktree}" in caplog.text
    assert f"Failed to delete stale task worktree directory {worktree}" in caplog.text


def test_attempt_launch_recovery_rebuilds_symlinked_task_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_workspace_repo(workspace)
    task = create_task(workspace, title="Recover task venv")
    worktree = resolve_task_execution_root(workspace, task)
    assert worktree.joinpath(".venv").is_symlink()
    real_run = subprocess.run

    def fake_sync(
        args: list[str], *, cwd: str, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        if args == ["uv", "sync", "--extra", "dev"]:
            assert capture_output is True and text is True and check is False
            Path(cwd, ".venv").mkdir()
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_run(args, cwd=cwd, capture_output=capture_output, text=text, check=check)

    with patch("litehive.recovery.execution_recovery.shutil.which", return_value="/usr/bin/uv"):
        with patch("litehive.recovery.execution_recovery.subprocess.run", side_effect=fake_sync):
            result = attempt_launch_recovery(
                workspace,
                get_task(workspace, task.id) or task,
                LaunchFailure(context="venv_sync_failed", summary=f"uv sync failed in {worktree}"),
            )

    assert result.fixed is True
    assert worktree.joinpath(".venv").is_dir()
    assert not worktree.joinpath(".venv").is_symlink()
    assert workspace.joinpath(".venv").is_dir()


def test_attempt_launch_recovery_cleans_first_launch_stale_worktree_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_workspace_repo(workspace)
    task = create_task(workspace, title="First launch stale worktree")
    stale_worktree = task_worktree_path(workspace, task)
    stale_worktree.mkdir(parents=True)
    stale_worktree.joinpath("blocked.txt").write_text("stale\n", encoding="utf-8")

    with pytest.raises(TaskLaunchFailure) as excinfo:
        prepare_task_launch(workspace, task)

    assert excinfo.value.context == "worktree_setup_failed"
    refreshed = get_task(workspace, task.id)
    assert refreshed is not None
    assert refreshed.runtime.git.worktree_path is None

    result = attempt_launch_recovery(workspace, refreshed, excinfo.value.as_failure())

    assert result.fixed is True
    assert result.actions[0].action == "reset_task_worktree"
    assert stale_worktree.exists() is False

    prepare_task_launch(workspace, refreshed)

    relaunched = get_task(workspace, task.id)
    assert relaunched is not None
    assert Path(relaunched.runtime.git.worktree_path) == stale_worktree
    assert stale_worktree.exists()
