import subprocess
from pathlib import Path

from litehive.config.paths import workspace_path
from litehive.config.workspace import create_workspace
from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.task import TaskRecord, UnmergedWorktree
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace
from litehive.worktree.cleanup import WorktreeCleanupService
from litehive.worktree.inspection import WorktreeInspector
from litehive.worktree.paths import serialize_worktree_path, task_worktree_branch
from litehive.worktree.rescue import WorktreeRescueService


def _git_ok(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc.stdout.strip()


def _bootstrap_git_workspace(workspace: Path) -> None:
    workspace.mkdir()
    _git_ok(workspace, "init", "-b", "main")
    _git_ok(workspace, "config", "user.email", "test@example.com")
    _git_ok(workspace, "config", "user.name", "Test User")
    create_workspace(workspace)
    (workspace / "app.txt").write_text("base\n", encoding="utf-8")
    _git_ok(workspace, "add", "app.txt")
    _git_ok(workspace, "commit", "-m", "initial")


def _add_task_worktree(workspace: Path, title: str) -> tuple[TaskRecord, Path]:
    workspace_obj = Workspace.from_path(workspace)
    task = WorkspaceTasks(workspace_obj).create( title=title, auto_commit=False)
    worktree_path = workspace_path(workspace, "worktrees") / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _git_ok(workspace, "worktree", "add", "-b", task_worktree_branch(task), str(worktree_path), "HEAD")
    task.runtime.pipeline.git.worktree_path = serialize_worktree_path(worktree_path)
    WorkspaceTasks(workspace_obj).save(task)
    return task, worktree_path


def test_collect_managed_worktrees_marks_active_task_without_crashing(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Tracked worktree")
    managed_worktree = workspace_path(tmp_path, "worktrees") / f"{task.id}-{task.slug}"
    managed_worktree.mkdir(parents=True)
    task.runtime.pipeline.git.worktree_path = str(managed_worktree.resolve())
    WorkspaceTasks(workspace).save(task)

    state = WorkspaceStateRepository(workspace).load()
    state.active_task_id = task.id
    WorkspaceStateRepository(workspace).save(state)

    worktrees = WorktreeCleanupService(workspace).collect_managed_worktrees()

    assert [item.task_id for item in worktrees] == [task.id]
    assert worktrees[0].active is True


def test_worktree_service_inspects_existing_worktree_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _bootstrap_git_workspace(workspace)
    workspace_obj = Workspace.from_path(workspace)
    task, worktree_path = _add_task_worktree(workspace, "Inspect task worktree")

    (worktree_path / "feature.txt").write_text("committed\n", encoding="utf-8")
    _git_ok(worktree_path, "add", "feature.txt")
    _git_ok(worktree_path, "commit", "-m", "feature commit")
    (worktree_path / "draft.txt").write_text("uncommitted\n", encoding="utf-8")

    inspection = WorktreeInspector(workspace_obj).inspect_task_worktree(task)

    assert inspection.task_id == task.id
    assert inspection.worktree_path == worktree_path.resolve()
    assert inspection.exists is True
    assert inspection.uncommitted == ["draft.txt"]
    assert inspection.committed_ahead_of_main == ["feature.txt"]


def test_worktree_service_cleanup_terminal_task_removes_worktree_and_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _bootstrap_git_workspace(workspace)
    workspace_obj = Workspace.from_path(workspace)
    task, worktree_path = _add_task_worktree(workspace, "Cleanup terminal worktree")
    task.status = TaskStatus.DONE
    task.pipeline_status = PipelineStatus.DONE
    WorkspaceTasks(workspace_obj).save(task)

    WorktreeCleanupService(workspace_obj).cleanup_terminal_task_worktree(task)

    refreshed = WorkspaceTasks(workspace_obj).get(task.id)
    assert refreshed is not None
    assert refreshed.runtime.pipeline.git.worktree_path is None
    assert not worktree_path.exists()
    assert _git_ok(workspace, "branch", "--list", task_worktree_branch(task)) == ""


def test_worktree_service_collects_rescue_candidates_from_unmerged_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _bootstrap_git_workspace(workspace)
    workspace_obj = Workspace.from_path(workspace)
    task, worktree_path = _add_task_worktree(workspace, "Collect rescue candidate")

    (worktree_path / "feature.txt").write_text("rescued\n", encoding="utf-8")
    _git_ok(worktree_path, "add", "feature.txt")
    _git_ok(worktree_path, "commit", "-m", "feature commit")
    commit_sha = _git_ok(worktree_path, "rev-parse", "HEAD")
    task.status = TaskStatus.FLAGGED
    task.pipeline_status = PipelineStatus.FLAGGED
    task.flag_reason = "merge_failed"
    WorkspaceTasks(workspace_obj).save(task)
    state = WorkspaceStateRepository(workspace_obj).load()
    state.unmerged_worktrees = [UnmergedWorktree(task_id=task.id, worktree_path=serialize_worktree_path(worktree_path))]
    WorkspaceStateRepository(workspace_obj).save(state)

    candidates = WorktreeRescueService(workspace_obj).collect_rescue_candidates()

    assert len(candidates) == 1
    assert candidates[0].task_id == task.id
    assert candidates[0].worktree_path == worktree_path.resolve()
    assert candidates[0].commit_shas == [commit_sha]
