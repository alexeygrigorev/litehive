"""WorktreeService: lifecycle/recovery/CLI entry point for worktree decisions.

Holds the create-or-reuse-and-rebase flow used at lifecycle pre-exec, plus
small wrappers that delegate to the cleanup, inspection, and rescue sibling
modules so callers can hold a single object instead of importing six free
functions.
"""

from pathlib import Path
from typing import Callable

from litehive.domain.task import TaskRecord
from litehive.domain.worktree import (
    ManagedWorktree,
    RescueCandidate,
    RescueResult,
    TaskWorktreeInspection,
    WorktreeMergeConflict,
    WorktreeSyncResult,
)
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
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    save_task,
    set_task_worktree_path,
)
from litehive.worktree.cleanup import (
    cleanup_terminal_task_worktree,
    collect_managed_worktrees,
    remove_cleanable_worktrees,
)
from litehive.worktree.inspection import worktree_committed_changes, worktree_uncommitted_changes
from litehive.worktree.paths import (
    ensure_worktree_venv_link,
    resolve_recorded_worktree_path,
    serialize_worktree_path,
    task_worktree_branch,
    task_worktree_path,
)
from litehive.worktree.rescue import apply_rescue_candidate, collect_rescue_candidates, require_clean_main_checkout


def status_porcelain_untracked(cwd: Path) -> bool:
    """Whether the worktree has any dirty entries including untracked files.

    Internal helper for ``WorktreeService._stash_local_changes`` so it can
    skip stashing when there is nothing to stash.
    """
    return bool(status_porcelain_with_options(cwd, include_ignored=False))


class WorktreeService:
    """Owns git/worktree decisions shared by lifecycle, recovery, and CLI."""

    def __init__(self, root: Path) -> None:
        """Bind the service to a single workspace root; every method below scopes its git operations under this directory so callers can hold one ``WorktreeService`` per workspace and forget about path threading."""
        self.root = Path(root)

    def sync_task_worktree(
        self,
        task_id: str,
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
        """Enumerate worktrees the workspace owns so the CLI status path can list them."""
        return collect_managed_worktrees(self.root)

    def remove_cleanable_worktrees(self, dry_run: bool = False) -> dict[str, list[ManagedWorktree]]:
        """Drop worktrees for completed/abandoned tasks; called by the cleanup CLI and recovery flows."""
        return remove_cleanable_worktrees(self.root, dry_run=dry_run)

    def collect_rescue_candidates(self) -> list[RescueCandidate]:
        """Find worktrees with unmerged work that need operator triage before deletion."""
        return collect_rescue_candidates(self.root)

    def apply_rescue_candidate(self, candidate: RescueCandidate) -> RescueResult:
        """Carry out a single rescue (stash/branch/merge) chosen by the recovery CLI."""
        return apply_rescue_candidate(self.root, candidate)

    def inspect_task_worktree(self, task: TaskRecord) -> TaskWorktreeInspection:
        """Snapshot a task's worktree state (existence, dirty files, ahead-of-main commits) for status/diagnostics readers."""
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
        """Detect a stale DB pointer to a worktree that was deleted out-of-band; recovery uses this to decide whether to re-create."""
        task = get_task(self.root, task_id)
        if task is None:
            return False
        inspection = self.inspect_task_worktree(task)
        return inspection.worktree_rel is not None and not inspection.exists

    def clear_missing_recorded_worktree(self, task_id: str) -> None:
        """Forget a recorded worktree path that no longer exists on disk so the next pre-exec creates a fresh one."""
        task = get_task(self.root, task_id)
        if task is None or not self.task_has_missing_recorded_worktree(task_id):
            return
        clear_task_worktree_path(task)
        save_task(self.root, task)

    def cleanup_terminal_task_worktree(self, task: TaskRecord) -> None:
        """Tear down the worktree once the task reaches a terminal pipeline state; called by the lifecycle finisher."""
        cleanup_terminal_task_worktree(self.root, task)

    def require_clean_main_checkout(self) -> None:
        """Guard merge/rescue flows that assume the main checkout has no uncommitted edits."""
        require_clean_main_checkout(self.root)

    def prune_stale_worktrees(self) -> None:
        """Force git to drop bookkeeping for worktrees whose directories disappeared, so re-create can reuse the branch name."""
        prune_worktrees(self.root, expire_now=True)

    def registered_worktree_for_branch(self, branch: str) -> Path | None:
        """Find an existing on-disk worktree git already tracks for ``branch`` so sync can reuse it instead of creating a duplicate."""
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
        """Pick the worktree path :func:`sync_task_worktree` should operate on, preferring an injected lifecycle resolver when one is provided so tests can redirect to a sandbox; falls back to the path recorded on the task otherwise."""
        if worktree_resolver is None:
            return recorded
        return Path(worktree_resolver(resolver_state))

    def _rebase_existing_worktree_onto_local_main(self, worktree: Path) -> bool:
        """Rebase a reused task worktree onto the workspace's current main HEAD before lifecycle pre-exec runs, returning True when HEAD actually moved; raises ``WorktreeMergeConflict`` for unresolved conflicts so the caller can surface them, and skips no-op rebases of the main checkout itself."""
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
        """Fetch and merge ``origin/<main>`` into the worktree, stashing local edits around the merge so an in-flight agent's working changes survive; returns True when the merge actually moved HEAD, raises ``WorktreeMergeConflict`` for unresolved conflicts, and aborts the merge before re-raising on other failures so the worktree is never left half-merged."""
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
        """Return the worktree's current HEAD SHA or None if undetermined; thin alias to ``current_head`` so the rebase flow can compare before/after without importing git ops directly."""
        return current_head(worktree)

    @staticmethod
    def _is_dirty(worktree: Path) -> bool:
        """Return True when the worktree has any tracked-file modifications; gates :func:`sync_task_worktree`'s decision to skip the origin merge so we don't clobber an agent's in-flight edits."""
        return has_changes(worktree)

    @staticmethod
    def _has_origin(worktree: Path) -> bool:
        """Return True when the worktree has an ``origin`` remote configured; gates :func:`sync_task_worktree`'s origin-merge step so workspaces without a remote are silently no-ops instead of erroring."""
        return remote_url(worktree, "origin") is not None

    @staticmethod
    def _unresolved(worktree: Path) -> list[str]:
        """Return paths git considers unmerged in the worktree; used by :func:`_rebase_existing_worktree_onto_local_main` and :func:`_merge_origin_main` to distinguish a real merge conflict (raise ``WorktreeMergeConflict``) from a generic git error."""
        return unmerged_files(worktree)

    @staticmethod
    def _stash_local_changes(worktree: Path) -> str | None:
        """Stash dirty entries (including untracked) and return the new ``refs/stash`` SHA so :func:`_merge_origin_main` can pop exactly that stash later, or None when there was nothing to stash; raises ``GitError`` on stash failure rather than silently dropping work."""
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
        """Pop the stash entry created by :func:`_stash_local_changes` after a successful origin merge; promotes a conflicted pop into ``WorktreeMergeConflict`` so the caller can surface the conflicting paths instead of leaving the agent staring at a silent stash."""
        if not stash_ref:
            return
        ok, message = stash_pop(worktree, ref=stash_ref, with_index=True)
        if ok:
            return
        unresolved = self._unresolved(worktree)
        if unresolved:
            raise WorktreeMergeConflict(unresolved)
        raise GitError(f"git stash pop failed: {message}")
