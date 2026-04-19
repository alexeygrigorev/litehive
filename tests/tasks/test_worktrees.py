import subprocess
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task
from litehive.tasks.worktrees import resolve_task_execution_root


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

    worktree = resolve_task_execution_root(workspace, task)

    assert worktree.joinpath(".venv").is_symlink()
    assert worktree.joinpath(".venv").resolve() == workspace.joinpath(".venv").resolve()
