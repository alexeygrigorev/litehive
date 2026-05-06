"""Resolve the directory a task's subagent should run in.

The runner calls :func:`resolve_task_execution_root` before spawning a
subagent: in a non-git workspace this is just the workspace root; in a
git workspace it's the task's dedicated worktree (created on first
call, reused on subsequent ones, rebased onto current ``main`` to keep
the agent on top of fresh code). A failed rebase lands in the
merge-resolver agent rather than aborting the task.
"""

import logging
from pathlib import Path

from litehive.agents.merge_resolver import run_worktree_merge_agent
from litehive.config.model import LitehiveConfig
from litehive.domain.task import TaskRecord
from litehive.fs_cleanup import remove_tree_logged
from litehive.git.ops import add_worktree, current_head, is_git_repo, rebase_worktree_onto
from litehive.state.records import (
    get_task_worktree_path,
    save_task,
    set_task_worktree_path,
)
from litehive.tasks.journal import append_journal
from litehive.workspace import Workspace
from litehive.worktree.paths import (
    ensure_worktree_venv_link,
    resolve_recorded_worktree_path,
    serialize_worktree_path,
    task_worktree_path,
)

logger = logging.getLogger(__name__)


def resolve_task_execution_root(
    root: Path,
    task: TaskRecord,
    config: LitehiveConfig | None = None,
) -> Path:
    """
    Pick the directory the task's subagent should run in.

    Used by the runner before spawning a subagent: in a non-git
    workspace this is just ``root``; in a git workspace it's the
    task's dedicated worktree (created on first call, reused on
    subsequent ones, rebased onto current ``main`` to keep the agent
    on top of fresh code). A failed rebase lands in the merge-resolver
    agent rather than aborting the task.
    """
    if not is_git_repo(root):
        return root

    workspace = Workspace.from_path(root)
    recorded_path = get_task_worktree_path(task)
    worktree_path = resolve_recorded_worktree_path(root, recorded_path)
    if worktree_path is not None:
        if not worktree_path.exists():
            set_task_worktree_path(task, None)
            save_task(root, task)
        else:
            main_head = current_head(root)
            if main_head:
                rebased = rebase_worktree_onto(worktree_path, main_head)
                if not rebased:
                    append_journal(
                        workspace,
                        task,
                        f"[worktree] Rebase onto {main_head[:8]} failed. Launching merge agent.",
                    )
                    run_worktree_merge_agent(workspace, worktree_path, task, main_head, config=config)
            return worktree_path

    worktree_path = task_worktree_path(root, task)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        remove_tree_logged(
            worktree_path,
            logger=logger,
            target_label="task worktree directory",
        )
    add_worktree(root, worktree_path, ref=current_head(root) or "HEAD")
    ensure_worktree_venv_link(root, worktree_path)
    set_task_worktree_path(task, serialize_worktree_path(worktree_path))
    save_task(root, task)
    append_journal(workspace, task, f"Created task worktree at `{get_task_worktree_path(task)}`.")
    return worktree_path
