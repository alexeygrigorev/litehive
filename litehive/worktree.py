"""Unified worktree lifecycle management and rescue operations.

This module consolidates worktree creation, inspection, listing, rescue, and cleanup
operations that were previously scattered across multiple modules.
"""

import logging
from pathlib import Path, PurePosixPath
from typing import Callable

from litehive.agents.merge_resolver import run_worktree_merge_agent
from litehive.config.model import LitehiveConfig
from litehive.domain.pool import DirtyWorktreeFinding, DirtyWorktreeGateReport
from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import WorkspaceConflictError
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
    is_managed_worktree_path,
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
    delete_branch,
    fetch as git_fetch,
    has_changes,
    is_git_repo,
    list_worktrees_porcelain,
    merge_abort,
    merge_no_edit,
    prune_worktrees,
    rebase_worktree_onto,
    remote_url,
    remove_worktree,
    rev_parse_verify,
    stash_pop,
    stash_push,
    status_porcelain,
    status_porcelain_with_options,
    stdout_lines as git_stdout_lines,
    stdout_or_none as git_stdout_or_none,
    unmerged_files,
)


from litehive.state.records import (
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    list_tasks,
    save_task,
    set_task_worktree_path,
)
from litehive.state.persist import load_state
from litehive.tasks.activity import load_task_activity
from litehive.tasks.activity_rendering import normalized_files_changed
from litehive.tasks.journal import append_journal
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
        return _collect_managed_worktrees(self.root)

    def remove_cleanable_worktrees(self, *, dry_run: bool = False) -> dict[str, list[ManagedWorktree]]:
        return _remove_cleanable_worktrees(self.root, dry_run=dry_run)

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
            uncommitted=_worktree_uncommitted_changes(worktree_path),
            committed_ahead_of_main=_worktree_committed_changes(self.root, worktree_path),
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
        _cleanup_terminal_task_worktree(self.root, task)

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


def git_worktree_blocks_pool(root: Path) -> bool:
    """Check if dirty worktrees block the pool."""
    return inspect_dirty_worktree_gate(root).blocks_pool


def inspect_dirty_worktree_gate(root: Path) -> DirtyWorktreeGateReport:
    """Inspect dirty worktrees and task ownership."""
    if not is_git_repo(root):
        return DirtyWorktreeGateReport()

    findings: list[DirtyWorktreeFinding] = []
    try:
        dirty_entries = status_porcelain(root)
    except GitError:
        return DirtyWorktreeGateReport()

    tasks = list_tasks(root, strict=False)
    if dirty_entries:
        owners = [task for task in tasks if _task_can_resume_with_owned_dirty_paths(root, task, dirty_entries)]
        finding = DirtyWorktreeFinding(
            location_kind="main-checkout",
            ownership="main-checkout",
            dirty_paths=_dirty_entry_paths(dirty_entries),
        )
        if len(owners) == 1:
            finding.ownership = "task-owned"
            finding.task_id = owners[0].id
            finding.worktree_path = get_task_worktree_path(owners[0])
        elif len(owners) > 1:
            finding.ownership = "ambiguous-ownership"
            finding.task_id = ",".join(task.id for task in owners)
        findings.append(finding)

    for task in tasks:
        worktree_path = resolve_recorded_worktree_path(root, get_task_worktree_path(task))
        if worktree_path is None:
            continue
        recorded_path = get_task_worktree_path(task)
        if not worktree_path.exists():
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=recorded_path,
                )
            )
            continue
        try:
            worktree_dirty_entries = status_porcelain(worktree_path)
        except GitError:
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=recorded_path,
                )
            )
            continue
        if not worktree_dirty_entries:
            continue
        findings.append(
            DirtyWorktreeFinding(
                location_kind="task-worktree",
                ownership="task-owned-worktree",
                task_id=task.id,
                worktree_path=recorded_path,
                dirty_paths=_dirty_entry_paths(worktree_dirty_entries),
            )
        )

    return DirtyWorktreeGateReport(findings=findings)


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


def cleanup_terminal_task_worktree(root: Path, task: TaskRecord) -> None:
    """Remove a terminal task's worktree and clear its metadata."""
    WorktreeService(root).cleanup_terminal_task_worktree(task)


def _cleanup_terminal_task_worktree(root: Path, task: TaskRecord) -> None:
    """Remove a terminal task's worktree and branch, then clear task metadata."""
    worktree_rel = get_task_worktree_path(task)
    if not worktree_rel:
        return
    worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
    if worktree_path is not None and worktree_path.exists():
        remove_worktree(root, worktree_path, force=True)
    clear_task_worktree_path(task)
    save_task(root, task)
    branch = task_worktree_branch(task)
    delete_branch(root, branch)


# === Worktree Discovery and Listing ===


def collect_managed_worktrees(root: Path) -> list[ManagedWorktree]:
    """Collect all Litehive-managed worktrees with their metadata."""
    return WorktreeService(root).collect_managed_worktrees()


def _collect_managed_worktrees(root: Path) -> list[ManagedWorktree]:
    """Collect all Litehive-managed worktrees with their metadata."""
    state = load_state(root)
    active_task = get_task(root, state.active_task_id) if state.active_task_id else None
    active_path = get_task_worktree_path(active_task) if active_task is not None else None

    worktrees: list[ManagedWorktree] = []
    for task in list_tasks(root, strict=False):
        worktree_rel = get_task_worktree_path(task)
        if not is_managed_worktree_path(root, worktree_rel):
            continue
        worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
        if worktree_path is None or not worktree_path.exists() or worktree_rel is None:
            continue
        try:
            change_count = len(status_porcelain(worktree_path))
        except GitError:
            change_count = 0
        worktrees.append(
            ManagedWorktree(
                task_id=task.id,
                status=task.status,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                change_count=change_count,
                active=task.id == state.active_task_id or worktree_rel == active_path,
            )
        )
    return sorted(worktrees, key=lambda item: item.task_id)


# === Worktree Cleanup ===


def remove_cleanable_worktrees(root: Path, *, dry_run: bool = False) -> dict[str, list[ManagedWorktree]]:
    """Remove cleanable worktrees and return categorized results."""
    return WorktreeService(root).remove_cleanable_worktrees(dry_run=dry_run)


def _remove_cleanable_worktrees(root: Path, *, dry_run: bool = False) -> dict[str, list[ManagedWorktree]]:
    """Remove cleanable worktrees and return categorized results."""
    worktrees = _collect_managed_worktrees(root)
    candidates = [item for item in worktrees if item.cleanable]
    skipped_active = [item for item in worktrees if item.active]

    if dry_run:
        return {
            "candidates": candidates,
            "skipped_active": skipped_active,
            "removed": [],
            "deferred": [],
            "failures": [],
        }

    failures = []
    removed = []
    deferred = []

    for item in candidates:
        try:
            remove_worktree(root, item.worktree_path, force=True)
            task = get_task(root, item.task_id)
            if task is not None:
                clear_task_worktree_path(task)
                try:
                    save_task(root, task)
                except WorkspaceConflictError:
                    from litehive.attention import append_attention_log  # noqa: PLC0415

                    append_attention_log(
                        root,
                        (f"deferred worktree metadata clearing for {item.task_id}: workspace locked by active runner"),
                    )
                    deferred.append(item)
                    continue
            removed.append(item)
        except GitError as exc:
            failures.append((item, str(exc)))

    return {
        "candidates": candidates,
        "skipped_active": skipped_active,
        "removed": removed,
        "deferred": deferred,
        "failures": failures,
    }


# Rescue operations live in ``litehive.worktree_rescue``. They are a
# self-contained cherry-pick flow that only the CLI rescue command and
# WorktreeService dispatch into.



# === Private Helper Functions ===


def _dirty_entry_paths(dirty_entries: list[str]) -> list[str]:
    """Extract file paths from git status --porcelain output."""
    paths: list[str] = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1].strip()
        if raw:
            paths.append(raw)
    return paths


def _worktree_uncommitted_changes(worktree_path: Path) -> list[str]:
    try:
        return sorted(set(_dirty_entry_paths(status_porcelain(worktree_path))))
    except GitError:
        return []


def _worktree_committed_changes(root: Path, worktree_path: Path) -> list[str]:
    main_head = current_head(root) or "HEAD"
    fork_point = git_stdout_or_none(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return []
    return sorted(set(git_stdout_lines(worktree_path, "diff", "--name-only", fork_point, "HEAD")))


def _allowed_commit_paths(root: Path, task: TaskRecord) -> set[PurePosixPath]:
    """Get the set of paths a task is allowed to commit."""
    paths: set[PurePosixPath] = set()
    paths.add(PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}")
    for entry in load_task_activity(root, task):
        for changed_file in normalized_files_changed(entry.files_changed):
            paths.add(PurePosixPath(changed_file))
    return paths


def _unexpected_dirty_paths(
    dirty_entries: list[str],
    allowed_paths: set[PurePosixPath],
) -> list[str]:
    """Get dirty paths that aren't allowed for this task."""
    unexpected = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if not raw:
            continue
        if "$tmpdir" in raw or raw.startswith("/tmp/"):
            continue
        if raw.startswith(".litehive/"):
            if not any(raw == str(path) or raw.startswith(f"{path}/") for path in allowed_paths):
                continue
        if any(raw == str(path) or raw.startswith(f"{path}/") for path in allowed_paths):
            continue
        unexpected.append(raw)
    return unexpected


def _task_can_resume_with_owned_dirty_paths(
    root: Path,
    task: TaskRecord,
    dirty_entries: list[str],
) -> bool:
    """Check if task can resume with these dirty paths."""
    if task.status != "interrupted":
        return False
    if task.pipeline_status in {"backlog", "done"}:
        return False
    return not _unexpected_dirty_paths(dirty_entries, _allowed_commit_paths(root, task))


def _remove_origin_remote(worktree_path: Path) -> None:
    """Remove origin remote from worktree (placeholder)."""
    _ = worktree_path


