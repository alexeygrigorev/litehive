import subprocess
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.recovery.launch import prepare_task_launch
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
