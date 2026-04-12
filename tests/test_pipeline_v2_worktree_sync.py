import subprocess
from pathlib import Path

from litehive.config import ensure_workspace, worktree_root
from litehive.pipeline.nodes.system import GitWorktreeSyncNode
from litehive.pipeline.persistence import TaskState
from litehive.pipeline.types import PipelineMode


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


def _state() -> TaskState:
    return TaskState(
        task_id="T-0001",
        stage="worktree_sync",
        pipeline_mode=PipelineMode.SINGLE,
    )


def test_worktree_sync_skips_dirty_worktrees(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / "workspace"
    upstream = tmp_path / "upstream"
    _git_ok(tmp_path, "init", "--bare", str(origin))

    seed.mkdir()
    _git_ok(seed, "init", "-b", "main")
    _configure_repo(seed)
    (seed / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(seed, "add", "app.txt")
    _git_ok(seed, "commit", "-m", "initial")
    _git_ok(seed, "remote", "add", "origin", str(origin))
    _git_ok(seed, "push", "-u", "origin", "main")

    _git_ok(tmp_path, "clone", str(origin), str(workspace))
    _configure_repo(workspace)
    ensure_workspace(workspace)
    worktree = worktree_root(workspace) / "T-0001-sync"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git_ok(workspace, "worktree", "add", "--detach", str(worktree), "HEAD")

    _git_ok(tmp_path, "clone", str(origin), str(upstream))
    _configure_repo(upstream)
    (upstream / "app.txt").write_text("base\nfrom origin\n", encoding="utf-8")
    _git_ok(upstream, "commit", "-am", "origin change")
    _git_ok(upstream, "push", "origin", "main")

    (worktree / "app.txt").write_text("base\nlocal draft\n", encoding="utf-8")

    node = GitWorktreeSyncNode(worktree_resolver=lambda state: worktree)
    changed = node._sync(_state())

    assert changed is False
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "base\nlocal draft\n"
    assert _git_ok(worktree, "status", "--porcelain") == "M app.txt"
    assert _git_ok(worktree, "stash", "list") == ""
