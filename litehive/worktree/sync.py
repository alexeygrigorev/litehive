"""
Task worktree provisioning and synchronization.

``WorktreeSyncService`` owns the lifecycle pre-exec flow: create or reuse the
task worktree, rebase it onto the current main checkout when resuming a stage,
and merge the configured upstream main ref when the worktree is clean.
"""

from pathlib import Path
from typing import Callable

from litehive.domain.worktree import WorktreeMergeConflict, WorktreeSyncResult
from litehive.git.ops import (
    GitError,
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
    WorkspaceTasks,
    get_task_worktree_path,
    set_task_worktree_path,
)
from litehive.worktree.paths import (
    serialize_worktree_path,
    task_worktree_branch,
    WorktreePaths,
)
from litehive.workspace import Workspace


def status_porcelain_untracked(cwd: Path) -> bool:
    """
    Whether the worktree has any dirty entries including untracked files.
    """
    return bool(status_porcelain_with_options(cwd, include_ignored=False))


class WorktreeSyncService:
    """
    Provision, reuse, and synchronize per-task git worktrees.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.tasks: WorkspaceTasks = WorkspaceTasks(workspace)
        self.paths = WorktreePaths(workspace)

    def sync_task_worktree(
        self,
        task_id: str,
        entry_stage: str | None,
        worktree_resolver: "Callable[..., Path] | None" = None,
        resolver_state: object | None = None,
        main_ref: str = "origin/main",
    ) -> WorktreeSyncResult:
        """
        Create, reuse, and sync a task worktree for lifecycle pre-exec.
        """
        if not is_git_repo(self.workspace.root):
            return WorktreeSyncResult(changed=False)

        task = self.tasks.get(task_id)
        if task is None:
            raise GitError(f"task {task_id} not found while creating worktree")

        recorded = self.paths.resolve_recorded_worktree_path(get_task_worktree_path(task))
        if recorded is None or not recorded.exists():
            branch = task_worktree_branch(task)
            existing = self.registered_worktree_for_branch(branch)
            if existing is not None:
                set_task_worktree_path(task, serialize_worktree_path(existing))
                self.tasks.save(task)
                recorded = existing
            else:
                worktree = self.paths.task_worktree_path(task)
                worktree.parent.mkdir(parents=True, exist_ok=True)
                self.prune_stale_worktrees()
                add_worktree_branch(self.workspace.root, branch, worktree, force=True)
                self.paths.ensure_venv_link(worktree)
                set_task_worktree_path(task, serialize_worktree_path(worktree))
                self.tasks.save(task)
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

    def prune_stale_worktrees(self) -> None:
        """
        Force git to drop bookkeeping for worktrees whose directories vanished.
        """
        prune_worktrees(self.workspace.root, expire_now=True)

    def registered_worktree_for_branch(self, branch: str) -> Path | None:
        """
        Find an existing on-disk worktree git already tracks for ``branch``.
        """
        porcelain = list_worktrees_porcelain(self.workspace.root)

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
        if worktree.resolve() == self.workspace.root.resolve():
            return False
        main_head = current_head(self.workspace.root)
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
