"""Resolve the directory a task's subagent should run in.

Task execution-root resolution: in a non-git workspace this is just the
workspace root; in a git workspace it's the task's dedicated worktree
(created on first call, reused on subsequent ones, rebased onto current
``main`` to keep the agent on top of fresh code). A failed rebase lands in
the merge-resolver agent rather than aborting the task.
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
    set_task_worktree_path,
    WorkspaceTasks,
)
from litehive.tasks.journal import append_journal
from litehive.worktree.paths import (
    serialize_worktree_path,
    WorktreePaths,
)
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)


class TaskExecutionRootResolver:
    """
    Workspace-bound resolver for the directory a task subagent should run in.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the resolver to one workspace and its path policy.

        The resolver holds a ``WorktreePaths`` instance so it can
        compute canonical worktree locations and ensure venv links
        without the caller constructing helpers on every invocation.
        """
        self.workspace = workspace
        self.paths = WorktreePaths(workspace)

    def resolve(self, task: TaskRecord, config: LitehiveConfig | None = None) -> Path:
        """
        Pick the directory the task's subagent should run in.
        """
        if not is_git_repo(self.workspace.root):
            return self.workspace.root

        merge_config = config or self.workspace.load_config()
        recorded_path = get_task_worktree_path(task)
        worktree_path = self.paths.resolve_recorded_worktree_path(recorded_path)
        if worktree_path is not None:
            if not worktree_path.exists():
                set_task_worktree_path(task, None)
                WorkspaceTasks(self.workspace).save(task)
            else:
                main_head = current_head(self.workspace.root)
                if main_head:
                    rebased = rebase_worktree_onto(worktree_path, main_head)
                    if not rebased:
                        append_journal(
                            self.workspace,
                            task,
                            f"[worktree] Rebase onto {main_head[:8]} failed. Launching merge agent.",
                        )
                        run_worktree_merge_agent(self.workspace, worktree_path, task, main_head, config=merge_config)
                return worktree_path

        worktree_path = self.paths.task_worktree_path(task)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        if worktree_path.exists():
            remove_tree_logged(
                worktree_path,
                logger=logger,
                target_label="task worktree directory",
            )
        add_worktree(self.workspace.root, worktree_path, ref=current_head(self.workspace.root) or "HEAD")
        self.paths.ensure_venv_link(worktree_path)
        set_task_worktree_path(task, serialize_worktree_path(worktree_path))
        WorkspaceTasks(self.workspace).save(task)
        append_journal(self.workspace, task, f"Created task worktree at `{get_task_worktree_path(task)}`.")
        return worktree_path
