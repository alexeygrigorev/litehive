from pathlib import Path

from litehive.config.paths import workspace_path
from litehive.config.workspace import create_workspace
from litehive.state.persist import load_state_for_workspace, save_state_for_workspace
from litehive.state.records import create_task_for_workspace, save_task_for_workspace
from litehive.workspace import Workspace
from litehive.worktree.cleanup import collect_managed_worktrees_for_workspace


def test_collect_managed_worktrees_marks_active_task_without_crashing(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Tracked worktree")
    managed_worktree = workspace_path(tmp_path, "worktrees") / f"{task.id}-{task.slug}"
    managed_worktree.mkdir(parents=True)
    task.runtime.pipeline.git.worktree_path = str(managed_worktree.resolve())
    save_task_for_workspace(workspace, task)

    state = load_state_for_workspace(workspace)
    state.active_task_id = task.id
    save_state_for_workspace(workspace, state)

    worktrees = collect_managed_worktrees_for_workspace(workspace)

    assert [item.task_id for item in worktrees] == [task.id]
    assert worktrees[0].active is True
