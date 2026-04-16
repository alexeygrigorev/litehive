import shutil
import subprocess
from pathlib import Path

from litehive.config.paths import worktree_root
from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.nodes.system import GitWorktreeSyncNode
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.worktrees import task_worktree_branch


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


def _state(task_id: str) -> TaskState:
    return TaskState(
        task_id=task_id,
        stage="worktree_sync",
        pipeline_mode=PipelineMode.SINGLE,
    )


def test_worktree_sync_creates_missing_task_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Sync")
    node = GitWorktreeSyncNode(
        workspace_root=workspace,
        worktree_resolver=lambda state: worktree_root(workspace) / f"{task.id}-{task.slug}",
    )

    changed = node._sync(_state(task.id))
    recorded_path = worktree_root(workspace) / f"{task.id}-{task.slug}"

    assert changed is True
    assert recorded_path.exists()
    assert _git_ok(recorded_path, "branch", "--show-current") == task_worktree_branch(task)


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
    task = create_task(workspace, title="Sync")
    worktree = worktree_root(workspace) / f"{task.id}-{task.slug}"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git_ok(
        workspace,
        "worktree",
        "add",
        "--force",
        "-B",
        task_worktree_branch(task),
        str(worktree),
        "HEAD",
    )
    task.runtime.git.worktree_path = str(worktree.resolve())
    save_task(workspace, task)

    _git_ok(tmp_path, "clone", str(origin), str(upstream))
    _configure_repo(upstream)
    (upstream / "app.txt").write_text("base\nfrom origin\n", encoding="utf-8")
    _git_ok(upstream, "commit", "-am", "origin change")
    _git_ok(upstream, "push", "origin", "main")

    (worktree / "app.txt").write_text("base\nlocal draft\n", encoding="utf-8")

    node = GitWorktreeSyncNode(
        workspace_root=workspace,
        worktree_resolver=lambda state: worktree,
    )
    changed = node._sync(_state(task.id))

    assert changed is False
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "base\nlocal draft\n"
    assert _git_ok(worktree, "status", "--porcelain") == "M app.txt"
    assert _git_ok(worktree, "stash", "list") == ""


def test_worktree_sync_prunes_stale_git_worktree_metadata_before_recreate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="QA verify venv symlink")
    worktree = worktree_root(workspace) / f"{task.id}-{task.slug}"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git_ok(
        workspace,
        "worktree",
        "add",
        "--force",
        "-B",
        task_worktree_branch(task),
        str(worktree),
        "HEAD",
    )
    task.runtime.git.worktree_path = None
    save_task(workspace, task)

    shutil.rmtree(worktree)

    node = GitWorktreeSyncNode(
        workspace_root=workspace,
        worktree_resolver=lambda state: worktree,
    )
    changed = node._sync(_state(task.id))

    assert changed is True
    assert worktree.exists()
    assert _git_ok(worktree, "branch", "--show-current") == task_worktree_branch(task)


def test_worktree_sync_reuses_existing_branch_worktree_when_runtime_path_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Recover existing task worktree")
    worktree = worktree_root(workspace) / f"{task.id}-{task.slug}"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git_ok(
        workspace,
        "worktree",
        "add",
        "--force",
        "-B",
        task_worktree_branch(task),
        str(worktree),
        "HEAD",
    )
    task.runtime.git.worktree_path = None
    save_task(workspace, task)

    node = GitWorktreeSyncNode(
        workspace_root=workspace,
        worktree_resolver=lambda state: worktree,
    )
    changed = node._sync(_state(task.id))

    refreshed = get_task(workspace, task.id)
    assert refreshed is not None
    assert changed is False
    assert refreshed.runtime.git.worktree_path == str(worktree.resolve())
    assert worktree.exists()
    assert _git_ok(worktree, "branch", "--show-current") == task_worktree_branch(task)
