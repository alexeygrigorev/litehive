"""Free functions and re-exports for the worktree subsystem.

The implementation is split across sibling modules:

* ``litehive.worktree_paths`` — pure path/branch arithmetic.
* ``litehive.worktree_inspection`` — read-only dirty-state and ownership reports.
* ``litehive.worktree_cleanup`` — listing and cleanup of managed worktrees.
* ``litehive.worktree_rescue`` — operator-driven cherry-pick onto main.
* ``litehive.worktree_service`` — ``WorktreeService`` create/reuse/sync flow.

This module hosts ``resolve_task_execution_root`` (which constructs the
execution root for a task) and continues to re-export common names so
existing call sites keep working.
"""

import logging
from pathlib import Path

from litehive.agents.merge_resolver import run_worktree_merge_agent
from litehive.config.model import LitehiveConfig
from litehive.domain.task import TaskRecord
from litehive.domain.worktree import WorktreeMergeConflict
from litehive.fs_cleanup import remove_tree_logged
from litehive.git.ops import add_worktree, current_head, is_git_repo, rebase_worktree_onto
from litehive.state.records import (
    get_task_worktree_path,
    save_task,
    set_task_worktree_path,
)
from litehive.tasks.journal import append_journal
from litehive.worktree_cleanup import (
    cleanup_terminal_task_worktree,
    collect_managed_worktrees,
    remove_cleanable_worktrees,
)
from litehive.worktree_inspection import inspect_dirty_worktree_gate
from litehive.worktree_paths import (
    ensure_worktree_venv_link,
    resolve_recorded_worktree_path,
    serialize_worktree_path,
    task_worktree_branch,
    task_worktree_path,
)
from litehive.worktree_rescue import (
    apply_rescue_candidate,
    collect_rescue_candidates,
    require_clean_main_checkout,
)
from litehive.worktree_service import WorktreeService, status_porcelain_untracked

__all__ = [
    "WorktreeMergeConflict",
    "WorktreeService",
    "apply_rescue_candidate",
    "cleanup_terminal_task_worktree",
    "collect_managed_worktrees",
    "collect_rescue_candidates",
    "ensure_worktree_venv_link",
    "inspect_dirty_worktree_gate",
    "remove_cleanable_worktrees",
    "require_clean_main_checkout",
    "resolve_recorded_worktree_path",
    "resolve_task_execution_root",
    "serialize_worktree_path",
    "status_porcelain_untracked",
    "task_worktree_branch",
    "task_worktree_path",
]

logger = logging.getLogger(__name__)


def resolve_task_execution_root(
    root: Path,
    task: TaskRecord,
    *,
    config: LitehiveConfig | None = None,
) -> Path:
    """Resolve or create the execution root for a task.

    Returns ``root`` when the workspace is not a git repository. Otherwise
    reuses or creates the task's worktree, rebasing it onto current main and
    launching the merge agent if the rebase fails.
    """
    if not is_git_repo(root):
        return root

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
                        root,
                        task,
                        f"[worktree] Rebase onto {main_head[:8]} failed. Launching merge agent.",
                    )
                    run_worktree_merge_agent(root, worktree_path, task, main_head, config=config)
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
    append_journal(root, task, f"Created task worktree at `{get_task_worktree_path(task)}`.")
    return worktree_path
