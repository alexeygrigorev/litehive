"""Unified worktree lifecycle management and rescue operations.

This module consolidates worktree creation, inspection, listing, rescue, and cleanup
operations that were previously scattered across multiple modules.
"""

import logging
from pathlib import Path
from typing import Callable

from litehive.agents.merge_resolver import run_worktree_merge_agent
from litehive.config.model import LitehiveConfig
from litehive.domain.task import TaskRecord
from litehive.domain.worktree import (
    ManagedWorktree,
    RescueCandidate,
    RescueResult,
    TaskWorktreeInspection,
    WorktreeMergeConflict,
    WorktreeSyncResult,
)
from litehive.fs_cleanup import remove_tree_logged
from litehive.worktree_paths import (
    ensure_worktree_venv_link,
    resolve_recorded_worktree_path,
    serialize_worktree_path,
    task_worktree_branch,
    task_worktree_path,
)
from litehive.git.ops import (
    GitError,
    add_worktree,
    add_worktree_branch,
    current_head,
    fetch as git_fetch,
    has_changes,
    is_git_repo,
    list_worktrees_porcelain,
    merge_abort,
    merge_no_edit,
    prune_worktrees,
    rebase_worktree_onto,
    remote_url,
    rev_parse_verify,
    stash_pop,
    stash_push,
    status_porcelain_with_options,
    unmerged_files,
)


from litehive.state.records import (
    clear_task_worktree_path,
    get_task,
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
from litehive.worktree_inspection import (
    worktree_committed_changes,
    worktree_uncommitted_changes,
)
from litehive.worktree_rescue import (
    apply_rescue_candidate,
    collect_rescue_candidates,
    require_clean_main_checkout,
)


def status_porcelain_untracked(cwd: Path) -> bool:
    """Whether the worktree has any dirty entries including untracked files."""
    return bool(status_porcelain_with_options(cwd, include_ignored=False))


logger = logging.getLogger(__name__)


class WorktreeService:
    """Owns git/worktree decisions shared by lifecycle, recovery, and CLI."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def sync_task_worktree(
        self,
        task_id: str,
        *,
        entry_stage: str | None,
        worktree_resolver: "Callable[[object], Path] | None" = None,
        resolver_state: object | None = None,
        main_ref: str = "origin/main",
    ) -> WorktreeSyncResult:
        """Create/reuse/sync a task worktree for lifecycle pre-exec."""
        if not is_git_repo(self.root):
            return WorktreeSyncResult(changed=False)

        task = get_task(self.root, task_id)
        if task is None:
            raise GitError(f"task {task_id} not found while creating worktree")

        recorded = resolve_recorded_worktree_path(self.root, get_task_worktree_path(task))
        if recorded is None or not recorded.exists():
            branch = task_worktree_branch(task)
            existing = self.registered_worktree_for_branch(branch)
            if existing is not None:
                set_task_worktree_path(task, serialize_worktree_path(existing))
                save_task(self.root, task)
                recorded = existing
            else:
                worktree = task_worktree_path(self.root, task)
                worktree.parent.mkdir(parents=True, exist_ok=True)
                self.prune_stale_worktrees()
                add_worktree_branch(self.root, branch, worktree, force=True)
                ensure_worktree_venv_link(self.root, worktree)
                set_task_worktree_path(task, serialize_worktree_path(worktree))
                save_task(self.root, task)
                return WorktreeSyncResult(changed=True, worktree_path=worktree)

        worktree = self._resolved_lifecycle_worktree(recorded, worktree_resolver, resolver_state)
        if not worktree.exists():
            return WorktreeSyncResult(changed=False, worktree_path=worktree)

        main_changed = False
        if entry_stage is not None:
            main_changed = self._rebase_existing_worktree_onto_local_main(worktree)

        if self._is_dirty(worktree):
            return WorktreeSyncResult(changed=main_changed, worktree_path=worktree)

        if not self._has_origin(worktree):
            return WorktreeSyncResult(changed=main_changed, worktree_path=worktree)

        changed = self._merge_origin_main(worktree, main_ref)
        return WorktreeSyncResult(changed=main_changed or changed, worktree_path=worktree)

    def collect_managed_worktrees(self) -> list[ManagedWorktree]:
        return collect_managed_worktrees(self.root)

    def remove_cleanable_worktrees(self, *, dry_run: bool = False) -> dict[str, list[ManagedWorktree]]:
        return remove_cleanable_worktrees(self.root, dry_run=dry_run)

    def collect_rescue_candidates(self) -> list[RescueCandidate]:
        return collect_rescue_candidates(self.root)

    def apply_rescue_candidate(self, candidate: RescueCandidate) -> RescueResult:
        return apply_rescue_candidate(self.root, candidate)

    def inspect_task_worktree(self, task: TaskRecord) -> TaskWorktreeInspection:
        worktree_rel = get_task_worktree_path(task)
        worktree_path = resolve_recorded_worktree_path(self.root, worktree_rel)
        if worktree_rel is None or worktree_path is None or not worktree_path.exists():
            return TaskWorktreeInspection(
                task_id=task.id,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                exists=False,
                uncommitted=[],
                committed_ahead_of_main=[],
            )
        return TaskWorktreeInspection(
            task_id=task.id,
            worktree_rel=worktree_rel,
            worktree_path=worktree_path,
            exists=True,
            uncommitted=worktree_uncommitted_changes(worktree_path),
            committed_ahead_of_main=worktree_committed_changes(self.root, worktree_path),
        )

    def task_has_missing_recorded_worktree(self, task_id: str) -> bool:
        task = get_task(self.root, task_id)
        if task is None:
            return False
        inspection = self.inspect_task_worktree(task)
        return inspection.worktree_rel is not None and not inspection.exists

    def clear_missing_recorded_worktree(self, task_id: str) -> None:
        task = get_task(self.root, task_id)
        if task is None or not self.task_has_missing_recorded_worktree(task_id):
            return
        clear_task_worktree_path(task)
        save_task(self.root, task)

    def cleanup_terminal_task_worktree(self, task: TaskRecord) -> None:
        cleanup_terminal_task_worktree(self.root, task)

    def require_clean_main_checkout(self) -> None:
        require_clean_main_checkout(self.root)

    def prune_stale_worktrees(self) -> None:
        prune_worktrees(self.root, expire_now=True)

    def registered_worktree_for_branch(self, branch: str) -> Path | None:
        porcelain = list_worktrees_porcelain(self.root)

        current_path: Path | None = None
        current_branch: str | None = None
        for raw_line in porcelain.splitlines():
            line = raw_line.strip()
            if not line:
                if current_branch == branch and current_path is not None and current_path.exists():
                    return current_path.resolve()
                current_path = None
                current_branch = None
                continue
            if line.startswith("worktree "):
                current_path = Path(line.removeprefix("worktree ").strip()).expanduser()
                continue
            if line.startswith("branch refs/heads/"):
                current_branch = line.removeprefix("branch refs/heads/").strip()

        if current_branch == branch and current_path is not None and current_path.exists():
            return current_path.resolve()
        return None

    def _resolved_lifecycle_worktree(
        self,
        recorded: Path,
        worktree_resolver: "Callable[[object], Path] | None",
        resolver_state: object | None,
    ) -> Path:
        if worktree_resolver is None:
            return recorded
        return Path(worktree_resolver(resolver_state))

    def _rebase_existing_worktree_onto_local_main(self, worktree: Path) -> bool:
        if worktree.resolve() == self.root.resolve():
            return False
        main_head = current_head(self.root)
        if main_head is None:
            return False
        before = self._head(worktree)
        rebased = rebase_worktree_onto(worktree, main_head)
        if rebased:
            after = self._head(worktree)
            return after is not None and after != before

        unresolved = self._unresolved(worktree)
        if unresolved:
            raise WorktreeMergeConflict(unresolved)
        raise GitError(f"worktree_sync rebase onto local main {main_head[:8]} failed")

    def _merge_origin_main(self, worktree: Path, main_ref: str) -> bool:
        stash_ref = self._stash_local_changes(worktree)
        restored_stash = False
        try:
            ok, fetch_message = git_fetch(worktree, "origin")
            if not ok:
                raise GitError(f"git fetch failed: {fetch_message}")

            merged, merge_message = merge_no_edit(worktree, main_ref)
            if merged:
                changed = "Already up to date" not in merge_message
                self._restore_local_changes(worktree, stash_ref)
                restored_stash = True
                return changed

            unresolved = self._unresolved(worktree)
            if unresolved:
                raise WorktreeMergeConflict(unresolved)

            merge_abort(worktree)
            self._restore_local_changes(worktree, stash_ref)
            restored_stash = True
            raise GitError(f"worktree_sync merge failed: {merge_message}")
        except Exception:
            if stash_ref and not restored_stash and not self._unresolved(worktree):
                self._restore_local_changes(worktree, stash_ref)
            raise

    @staticmethod
    def _head(worktree: Path) -> str | None:
        return current_head(worktree)

    @staticmethod
    def _is_dirty(worktree: Path) -> bool:
        return has_changes(worktree)

    @staticmethod
    def _has_origin(worktree: Path) -> bool:
        return remote_url(worktree, "origin") is not None

    @staticmethod
    def _unresolved(worktree: Path) -> list[str]:
        return unmerged_files(worktree)

    @staticmethod
    def _stash_local_changes(worktree: Path) -> str | None:
        if not status_porcelain_untracked(worktree):
            return None
        before_ref = rev_parse_verify(worktree, "refs/stash") or ""
        ok, message = stash_push(worktree, "litehive-worktree-sync", include_untracked=True)
        if not ok:
            raise GitError(f"git stash push failed: {message}")
        after_ref = rev_parse_verify(worktree, "refs/stash") or ""
        if not after_ref or after_ref == before_ref:
            return None
        return after_ref

    def _restore_local_changes(self, worktree: Path, stash_ref: str | None) -> None:
        if not stash_ref:
            return
        ok, message = stash_pop(worktree, ref=stash_ref, with_index=True)
        if ok:
            return
        unresolved = self._unresolved(worktree)
        if unresolved:
            raise WorktreeMergeConflict(unresolved)
        raise GitError(f"git stash pop failed: {message}")


# Path utilities live in ``litehive.worktree_paths``. They are pure path
# arithmetic with no git or state dependencies, so they belong in their
# own sibling module and many callers only need them.


def resolve_task_execution_root(
    root: Path,
    task: TaskRecord,
    *,
    config: LitehiveConfig | None = None,
) -> Path:
    """Resolve or create the execution root for a task (worktree if git repo, main if not)."""
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
            _remove_origin_remote(worktree_path)
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
    _remove_origin_remote(worktree_path)
    set_task_worktree_path(task, serialize_worktree_path(worktree_path))
    save_task(root, task)
    append_journal(root, task, f"Created task worktree at `{get_task_worktree_path(task)}`.")
    return worktree_path


# Cleanup and discovery operations (cleanup_terminal_task_worktree,
# collect_managed_worktrees, remove_cleanable_worktrees) live in
# ``litehive.worktree_cleanup``. Rescue operations
# (collect_rescue_candidates, require_clean_main_checkout,
# apply_rescue_candidate) live in ``litehive.worktree_rescue``. Both are
# self-contained and can be imported without pulling in WorktreeService.



def _remove_origin_remote(worktree_path: Path) -> None:
    """Remove origin remote from worktree (placeholder)."""
    _ = worktree_path


