"""
``WorktreeService``: the worktree-decisions entry point shared by lifecycle, recovery, and CLI.

Owns the create-or-reuse-and-rebase flow used at lifecycle pre-exec
(``sync_task_worktree``) and exposes thin instance-method wrappers
over the cleanup/inspection/rescue sibling modules so callers can
hold one ``WorktreeService`` per workspace instead of importing six
free functions. The free-function form still exists in the siblings;
this class is a convenience facade, not a re-implementation.
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
    WorktreeCleanupResult,
    cleanup_terminal_task_worktree,
    collect_managed_worktrees,
    remove_cleanable_worktrees_for_workspace,
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
    """
    Whether the worktree has any dirty entries including untracked files.

    Internal helper for ``WorktreeService._stash_local_changes`` so
    the stash step can skip when there's nothing to stash — running
    ``git stash push`` on a clean tree creates a no-op stash entry
    that pollutes the stash list and confuses recovery.
    """
    return bool(status_porcelain_with_options(cwd, include_ignored=False))


class WorktreeService:
    """
    Worktree decisions shared by lifecycle, recovery, and the worktree CLI.

    One instance per workspace; methods scope all git operations
    under ``self.root``. ``sync_task_worktree`` is the heart —
    everything else is either a thin wrapper over a sibling module
    or a private rebase/merge helper. Lifecycle pre-exec drives
    sync; recovery uses the cleanup/inspection helpers; the CLI
    drives rescue.
    """

    def __init__(self, root: Path) -> None:
        """
        Bind the service to a single workspace root.

        Every method below scopes its git operations under this
        directory so callers can hold one ``WorktreeService`` per
        workspace and forget about path threading. The ``Path()``
        wrap normalizes the input so equality checks downstream
        don't fail on a string vs. a ``Path``.
        """
        self.root = Path(root)

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

        Three branches: create when nothing is recorded or the
        recorded path is gone (``add_worktree_branch`` plus venv
        link), reuse when the recorded worktree exists (rebase onto
        local ``main`` and optionally merge ``origin/<main>``), and
        no-op when the workspace is not a git repo. Conflict during
        rebase or merge raises ``WorktreeMergeConflict`` so the
        merge-resolver lifecycle node can pick up the conflict
        files — that exception is the contract between this
        function and the merge-resolver agent.
        """
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
        """
        Enumerate worktrees the workspace owns so the CLI status path can list them.

        Thin instance wrapper over :func:`collect_managed_worktrees`
        so callers holding a service handle don't import the cleanup
        module just to list.
        """
        return collect_managed_worktrees(self.root)

    def remove_cleanable_worktrees(self, dry_run: bool = False) -> WorktreeCleanupResult:
        """
        Drop worktrees for completed/abandoned tasks.

        Used by ``litehive worktree clean`` and the recovery flow's
        post-success cleanup. ``dry_run`` lets the CLI preview what
        would be removed without touching disk.
        """
        from litehive.workspace import Workspace  # noqa: PLC0415

        return remove_cleanable_worktrees_for_workspace(Workspace.from_path(self.root), dry_run=dry_run)

    def collect_rescue_candidates(self) -> list[RescueCandidate]:
        """
        Find worktrees with unmerged work needing operator triage before deletion.

        Pairs with :meth:`apply_rescue_candidate`; the rescue CLI
        lists candidates first so the operator can review before
        applying.
        """
        return collect_rescue_candidates(self.root)

    def apply_rescue_candidate(self, candidate: RescueCandidate) -> RescueResult:
        """
        Carry out a single rescue chosen by the rescue CLI.

        Cherry-picks the candidate's commits onto main with metadata
        scrub and finalization. Caller must have run
        :meth:`require_clean_main_checkout` once for the batch.
        """
        return apply_rescue_candidate(self.root, candidate)

    def inspect_task_worktree(self, task: TaskRecord) -> TaskWorktreeInspection:
        """
        Snapshot a task's worktree state for status and diagnostics readers.

        Bundles existence, uncommitted changes, and committed-past-
        main paths into a single ``TaskWorktreeInspection`` so the
        operator's status output renders all three from one helper
        call instead of three separate git invocations.
        """
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
        """
        Detect a stale task→worktree pointer that no longer maps to a directory.

        Recovery uses this before deciding whether to re-create the
        worktree from scratch — without the check, recovery would
        try to reuse a path that is no longer on disk and fail
        unpredictably during a rebase.
        """
        task = get_task(self.root, task_id)
        if task is None:
            return False
        inspection = self.inspect_task_worktree(task)
        return inspection.worktree_rel is not None and not inspection.exists

    def clear_missing_recorded_worktree(self, task_id: str) -> None:
        """
        Forget a recorded worktree path that no longer exists on disk.

        Lets the next pre-exec create a fresh worktree instead of
        repeatedly trying to use a stale pointer. Idempotent — a
        no-op when the recorded path is still valid, so callers can
        invoke speculatively.
        """
        task = get_task(self.root, task_id)
        if task is None or not self.task_has_missing_recorded_worktree(task_id):
            return
        clear_task_worktree_path(task)
        save_task(self.root, task)

    def cleanup_terminal_task_worktree(self, task: TaskRecord) -> None:
        """
        Tear down the worktree once the task reaches a terminal pipeline state.

        Called by the lifecycle finisher on done/closed/cancelled
        transitions; thin wrapper over the free function in
        ``litehive.worktree.cleanup``.
        """
        cleanup_terminal_task_worktree(self.root, task)

    def require_clean_main_checkout(self) -> None:
        """
        Guard merge/rescue flows that assume a clean main checkout.

        Called once before a batch of rescue applies so dirty edits
        from the operator can't get spliced into the rescued
        commits. Raises ``GitError`` to halt the batch loudly
        rather than performing a partial rescue.
        """
        require_clean_main_checkout(self.root)

    def prune_stale_worktrees(self) -> None:
        """
        Force git to drop bookkeeping for worktrees whose directories vanished.

        Without this, ``git worktree add`` refuses to reuse a
        branch name git still associates with a deleted directory,
        and the next pre-exec for the same task would error out.
        ``--expire now`` skips the default grace period so cleanup
        is immediate.
        """
        prune_worktrees(self.root, expire_now=True)

    def registered_worktree_for_branch(self, branch: str) -> Path | None:
        """
        Find an existing on-disk worktree git already tracks for ``branch``.

        Lets sync reuse a worktree git knows about instead of
        adding a duplicate when the task record has lost the
        recorded path but git still has the branch checked out
        somewhere. Returns ``None`` if no live worktree matches.
        """
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
        """
        Pick the worktree path :meth:`sync_task_worktree` should operate on.

        Prefers an injected lifecycle resolver when one is supplied
        so tests can redirect sync to a sandboxed path without
        rewriting the recorded task pointer; falls back to the
        recorded path otherwise. Without the indirection, tests
        would have to monkey-patch ``Path`` to redirect.
        """
        if worktree_resolver is None:
            return recorded
        return Path(worktree_resolver(resolver_state))

    def _rebase_existing_worktree_onto_local_main(self, worktree: Path) -> bool:
        """
        Rebase a reused task worktree onto current local ``main`` before pre-exec.

        Returns ``True`` when HEAD actually moved so the caller
        can surface "the workspace shifted" to the agent. Raises
        ``WorktreeMergeConflict`` for unresolved conflicts so the
        merge-resolver lifecycle node can pick up the file list,
        and skips the no-op self-rebase when the worktree path
        IS the main checkout (some single-task workflows reuse
        main directly).
        """
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
        """
        Fetch and merge ``origin/<main>`` into the worktree.

        Stashes local edits around the merge so an in-flight agent's
        working changes survive a sync. Returns ``True`` when the
        merge actually moved HEAD. Raises ``WorktreeMergeConflict``
        on unresolved conflicts so the merge-resolver node can take
        over; aborts the merge before re-raising on other failures
        so the worktree is never left half-merged for the next
        sync to trip over.
        """
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
        """
        Worktree HEAD SHA or ``None`` if undetermined.

        Thin alias to ``current_head`` so the rebase flow can
        compare before/after HEADs without importing the git ops
        module directly at every site.
        """
        return current_head(worktree)

    @staticmethod
    def _is_dirty(worktree: Path) -> bool:
        """
        True when the worktree has any tracked-file modifications.

        Gates :meth:`sync_task_worktree`'s decision to skip the
        origin merge — merging on top of an agent's in-flight
        edits would clobber work the agent intended to keep.
        """
        return has_changes(worktree)

    @staticmethod
    def _has_origin(worktree: Path) -> bool:
        """
        True when the worktree has an ``origin`` remote configured.

        Gates :meth:`sync_task_worktree`'s origin-merge step so a
        workspace without an ``origin`` is silently a no-op
        instead of erroring on every sync. Local-only workspaces
        are a real use case (single-machine ops, demo setups).
        """
        return remote_url(worktree, "origin") is not None

    @staticmethod
    def _unresolved(worktree: Path) -> list[str]:
        """
        Return paths git considers unmerged in the worktree.

        Used by :meth:`_rebase_existing_worktree_onto_local_main`
        and :meth:`_merge_origin_main` to distinguish a real merge
        conflict (raise ``WorktreeMergeConflict`` so the resolver
        node can act) from a generic git error (just raise
        ``GitError``).
        """
        return unmerged_files(worktree)

    @staticmethod
    def _stash_local_changes(worktree: Path) -> str | None:
        """
        Stash dirty entries (including untracked) before a sync merge.

        Returns the new ``refs/stash`` SHA so
        :meth:`_merge_origin_main` can pop exactly that stash
        later, even if another sync runs concurrently and pushes
        a new stash on top. Returns ``None`` when there was
        nothing to stash. Raises ``GitError`` on stash failure
        rather than silently dropping work — losing in-flight
        agent edits would be much worse than failing the sync.
        """
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
        """
        Pop the stash entry from :meth:`_stash_local_changes` after the merge.

        Promotes a conflicted pop into ``WorktreeMergeConflict`` so
        the caller can surface the conflicting paths to the
        merge-resolver node — leaving the stash silently sitting
        in the stash list while the worktree looks clean would
        let the next sync run on top of forgotten edits.
        """
        if not stash_ref:
            return
        ok, message = stash_pop(worktree, ref=stash_ref, with_index=True)
        if ok:
            return
        unresolved = self._unresolved(worktree)
        if unresolved:
            raise WorktreeMergeConflict(unresolved)
        raise GitError(f"git stash pop failed: {message}")
