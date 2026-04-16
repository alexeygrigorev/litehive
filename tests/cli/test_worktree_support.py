from pathlib import Path

from litehive.cli.worktree_support import collect_managed_worktrees
from litehive.config.paths import worktree_root
from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, save_task


def test_collect_managed_worktrees_marks_active_task_without_crashing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tracked worktree")
    managed_worktree = worktree_root(tmp_path) / f"{task.id}-{task.slug}"
    managed_worktree.mkdir(parents=True)
    task.runtime.git.worktree_path = str(managed_worktree.resolve())
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    worktrees = collect_managed_worktrees(tmp_path)

    assert [item.task_id for item in worktrees] == [task.id]
    assert worktrees[0].active is True
