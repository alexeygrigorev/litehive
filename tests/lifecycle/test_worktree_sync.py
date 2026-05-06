import shutil
import subprocess
from pathlib import Path

from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.domain.common import PipelineState
from litehive.lifecycle.nodes.system import GitWorktreeSyncNode
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.state.records import create_task, get_task, save_task
from litehive.workspace import Workspace
from litehive.worktree.paths import task_worktree_branch


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


def _state(task_id: str, *, entry_stage: PipelineState | None = None) -> TaskState:
    return TaskState(
        task_id=task_id,
        stage=PipelineState.WORKTREE_SYNC,
        pipeline_mode=PipelineMode.SINGLE,
        entry_stage=entry_stage,
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
        workspace=Workspace.from_path(workspace),
        worktree_resolver=lambda state: workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}",
    )

    changed = node.sync(_state(task.id))
    recorded_path = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"

    assert changed is True
    assert recorded_path.exists()
    assert _git_ok(recorded_path, "branch", "--show-current") == task_worktree_branch(task)


def test_worktree_sync_links_worktree_venv_to_workspace_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    (workspace / ".venv").mkdir()
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Shared venv")
    node = GitWorktreeSyncNode(
        workspace=Workspace.from_path(workspace),
        worktree_resolver=lambda state: workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}",
    )

    changed = node.sync(_state(task.id))
    worktree = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"

    assert changed is True
    assert worktree.joinpath(".venv").is_symlink()
    assert worktree.joinpath(".venv").resolve() == workspace.joinpath(".venv").resolve()


def test_worktree_sync_skips_broken_venv_link_when_workspace_has_no_venv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="No shared venv")
    node = GitWorktreeSyncNode(
        workspace=Workspace.from_path(workspace),
        worktree_resolver=lambda state: workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}",
    )

    changed = node.sync(_state(task.id))
    worktree = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"

    assert changed is True
    assert not worktree.joinpath(".venv").exists()
    assert not worktree.joinpath(".venv").is_symlink()


def test_worktree_sync_rebases_existing_task_worktree_onto_local_main(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Rebase before resume")
    worktree = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
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
    task.runtime.pipeline.git.worktree_path = str(worktree.resolve())
    save_task(workspace, task)

    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git_ok(worktree, "add", "feature.txt")
    _git_ok(worktree, "commit", "-m", "feature")
    feature_head_before = _git_ok(worktree, "rev-parse", "HEAD")

    (workspace / "infra.txt").write_text("main advanced\n", encoding="utf-8")
    _git_ok(workspace, "add", "infra.txt")
    _git_ok(workspace, "commit", "-m", "main advanced")
    main_head = _git_ok(workspace, "rev-parse", "HEAD")

    node = GitWorktreeSyncNode(
        workspace=Workspace.from_path(workspace),
        worktree_resolver=lambda state: worktree,
    )
    changed = node.sync(_state(task.id, entry_stage=PipelineState.IMPLEMENTING))

    assert changed is True
    assert (worktree / "infra.txt").read_text(encoding="utf-8") == "main advanced\n"
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert _git(worktree, "merge-base", "--is-ancestor", main_head, "HEAD").returncode == 0
    assert _git_ok(worktree, "rev-parse", "HEAD") != feature_head_before
    assert _git_ok(worktree, "status", "--porcelain") == ""


def test_worktree_sync_rebases_dirty_resumed_worktree_and_preserves_wip(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _configure_repo(workspace)
    ensure_workspace(workspace)

    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")

    task = create_task(workspace, title="Dirty resume")
    worktree = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
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
    task.runtime.pipeline.git.worktree_path = str(worktree.resolve())
    save_task(workspace, task)

    (worktree / "app.txt").write_text("base\nlocal draft\n", encoding="utf-8")
    (worktree / "notes.txt").write_text("untracked draft\n", encoding="utf-8")

    (workspace / "infra.txt").write_text("main advanced\n", encoding="utf-8")
    _git_ok(workspace, "add", "infra.txt")
    _git_ok(workspace, "commit", "-m", "main advanced")
    main_head = _git_ok(workspace, "rev-parse", "HEAD")

    node = GitWorktreeSyncNode(
        workspace=Workspace.from_path(workspace),
        worktree_resolver=lambda state: worktree,
    )
    changed = node.sync(_state(task.id, entry_stage=PipelineState.TESTING))

    assert changed is True
    assert (worktree / "infra.txt").read_text(encoding="utf-8") == "main advanced\n"
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "base\nlocal draft\n"
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "untracked draft\n"
    assert _git(worktree, "merge-base", "--is-ancestor", main_head, "HEAD").returncode == 0
    assert _git_ok(worktree, "status", "--porcelain") == "M app.txt\n?? notes.txt"
    assert _git_ok(worktree, "stash", "list") == ""


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
    worktree = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
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
    task.runtime.pipeline.git.worktree_path = str(worktree.resolve())
    save_task(workspace, task)

    _git_ok(tmp_path, "clone", str(origin), str(upstream))
    _configure_repo(upstream)
    (upstream / "app.txt").write_text("base\nfrom origin\n", encoding="utf-8")
    _git_ok(upstream, "commit", "-am", "origin change")
    _git_ok(upstream, "push", "origin", "main")

    (worktree / "app.txt").write_text("base\nlocal draft\n", encoding="utf-8")

    node = GitWorktreeSyncNode(
        workspace=Workspace.from_path(workspace),
        worktree_resolver=lambda state: worktree,
    )
    changed = node.sync(_state(task.id))

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
    worktree = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
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
    task.runtime.pipeline.git.worktree_path = None
    save_task(workspace, task)

    shutil.rmtree(worktree)

    node = GitWorktreeSyncNode(
        workspace=Workspace.from_path(workspace),
        worktree_resolver=lambda state: worktree,
    )
    changed = node.sync(_state(task.id))

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
    worktree = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
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
    task.runtime.pipeline.git.worktree_path = None
    save_task(workspace, task)

    node = GitWorktreeSyncNode(
        workspace=Workspace.from_path(workspace),
        worktree_resolver=lambda state: worktree,
    )
    changed = node.sync(_state(task.id))

    refreshed = get_task(workspace, task.id)
    assert refreshed is not None
    assert changed is False
    assert refreshed.runtime.pipeline.git.worktree_path == str(worktree.resolve())
    assert worktree.exists()
    assert _git_ok(worktree, "branch", "--show-current") == task_worktree_branch(task)
