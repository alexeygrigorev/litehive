"""Unified worktree lifecycle management and rescue operations.

This module consolidates worktree creation, inspection, listing, rescue, and cleanup
operations that were previously scattered across multiple modules.
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from litehive.agents.merge_resolver import run_worktree_merge_agent
from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.config.paths import workspace_path
from litehive.domain.pool import DirtyWorktreeFinding, DirtyWorktreeGateReport
from litehive.domain.task import TaskRecord, UnmergedWorktree
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.fs_cleanup import remove_tree_logged
from litehive.git.ops import (
    GitError,
    add_worktree,
    current_head,
    has_non_litehive_changes,
    is_git_repo,
    rebase_worktree_onto,
    remove_worktree,
    status_porcelain,
)
from litehive.state.records import (
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    list_tasks,
    save_task,
    set_task_commit_sha,
    set_task_worktree_path,
)
from litehive.state.persist import load_state, save_state
from litehive.tasks.activity import load_task_activity
from litehive.tasks.journal import append_journal
from litehive.tasks.activity_rendering import normalized_files_changed

logger = logging.getLogger(__name__)

# Constants
_CLEANABLE_STATUSES = {"closed", "done"}


@dataclass(slots=True)
class ManagedWorktree:
    """Information about a Litehive-managed task worktree."""

    task_id: str
    status: str
    worktree_rel: str
    worktree_path: Path
    change_count: int
    active: bool

    @property
    def cleanable(self) -> bool:
        return self.status in _CLEANABLE_STATUSES and not self.active


@dataclass(slots=True)
class RescueCandidate:
    """A worktree that may need rescue (cherry-pick to main)."""

    task_id: str
    worktree_rel: str
    worktree_path: Path
    commit_shas: list[str]


@dataclass(slots=True)
class RescueResult:
    """Result of attempting to rescue a worktree."""

    task_id: str
    worktree_rel: str
    status: str
    commit_shas: list[str]
    head_sha: str | None = None
    message: str | None = None


class WorktreeMergeConflict(Exception):
    """Raised when worktree sync leaves unresolved files behind."""

    def __init__(self, conflict_files: list[str]) -> None:
        super().__init__(f"{len(conflict_files)} unresolved file(s)")
        self.conflict_files = conflict_files


@dataclass(slots=True)
class WorktreeSyncResult:
    """Outcome of pre-exec lifecycle worktree synchronization."""

    changed: bool
    worktree_path: Path | None = None


@dataclass(slots=True)
class TaskWorktreeInspection:
    """Inspectable status for a task's recorded worktree."""

    task_id: str
    worktree_rel: str | None
    worktree_path: Path | None
    exists: bool
    uncommitted: list[str]
    committed_ahead_of_main: list[str]


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
                created = subprocess.run(
                    ["git", "worktree", "add", "--force", "-B", branch, str(worktree), "HEAD"],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                )
                if created.returncode != 0:
                    raise GitError(f"git worktree add failed: {created.stderr.strip() or created.stdout.strip()}")
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
        return _collect_rescue_candidates(self.root)

    def apply_rescue_candidate(self, candidate: RescueCandidate) -> RescueResult:
        return _apply_rescue_candidate(self.root, candidate)

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
        _require_clean_main_checkout(self.root)

    def prune_stale_worktrees(self) -> None:
        proc = subprocess.run(
            ["git", "worktree", "prune", "--expire", "now"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"git worktree prune failed: {proc.stderr.strip() or proc.stdout.strip()}")

    def registered_worktree_for_branch(self, branch: str) -> Path | None:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"git worktree list failed: {proc.stderr.strip() or proc.stdout.strip()}")

        current_path: Path | None = None
        current_branch: str | None = None
        for raw_line in proc.stdout.splitlines():
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
            fetch = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                raise GitError(f"git fetch failed: {fetch.stderr.strip() or fetch.stdout.strip()}")

            merge = subprocess.run(
                ["git", "merge", main_ref, "--no-edit"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
            )
            if merge.returncode == 0:
                changed = "Already up to date" not in merge.stdout
                self._restore_local_changes(worktree, stash_ref)
                restored_stash = True
                return changed

            unresolved = self._unresolved(worktree)
            if unresolved:
                raise WorktreeMergeConflict(unresolved)

            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
            )
            self._restore_local_changes(worktree, stash_ref)
            restored_stash = True
            raise GitError(f"worktree_sync merge failed: {merge.stderr.strip() or merge.stdout.strip()}")
        except Exception:
            if stash_ref and not restored_stash and not self._unresolved(worktree):
                self._restore_local_changes(worktree, stash_ref)
            raise

    @staticmethod
    def _head(worktree: Path) -> str | None:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    @staticmethod
    def _is_dirty(worktree: Path) -> bool:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())

    @staticmethod
    def _has_origin(worktree: Path) -> bool:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())

    @staticmethod
    def _unresolved(worktree: Path) -> list[str]:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    @staticmethod
    def _stash_local_changes(worktree: Path) -> str | None:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if status.returncode != 0 or not status.stdout.strip():
            return None
        before = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "refs/stash"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        stash = subprocess.run(
            ["git", "stash", "push", "-u", "-m", "litehive-worktree-sync"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if stash.returncode != 0:
            raise GitError(f"git stash push failed: {stash.stderr.strip() or stash.stdout.strip()}")
        after = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "refs/stash"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        before_ref = before.stdout.strip()
        after_ref = after.stdout.strip()
        if not after_ref or after_ref == before_ref:
            return None
        return after_ref

    def _restore_local_changes(self, worktree: Path, stash_ref: str | None) -> None:
        if not stash_ref:
            return
        restored = subprocess.run(
            ["git", "stash", "pop", "--index", stash_ref],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        if restored.returncode == 0:
            return
        unresolved = self._unresolved(worktree)
        if unresolved:
            raise WorktreeMergeConflict(unresolved)
        raise GitError(f"git stash pop failed: {restored.stderr.strip() or restored.stdout.strip()}")


# === Path Utilities ===


def task_worktree_path(root: Path, task: TaskRecord) -> Path:
    """Get the expected worktree path for a task."""
    return workspace_path(root, "worktrees") / f"{task.id}-{task.slug}"


def task_worktree_branch(task: TaskRecord) -> str:
    """Get the branch name for a task worktree."""
    return f"litehive/{task.id}-{task.slug}"


def is_managed_worktree_path(root: Path, worktree_path: str | None) -> bool:
    """Check if a worktree path is managed by Litehive."""
    if not worktree_path:
        return False
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        return False
    try:
        return path.resolve().is_relative_to(workspace_path(root, "worktrees").resolve())
    except OSError:
        return False


def resolve_recorded_worktree_path(root: Path, worktree_path: str | None) -> Path | None:
    """Resolve a recorded worktree path to an absolute path."""
    if not worktree_path:
        return None
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def serialize_worktree_path(path: Path) -> str:
    """Serialize a worktree path for storage."""
    return str(path.expanduser().resolve())


# === Worktree Lifecycle ===


def ensure_worktree_venv_link(root: Path, worktree_path: Path) -> Path | None:
    """Ensure the worktree has a venv symlink to the main venv."""
    main_venv = (root / ".venv").expanduser()
    if not (main_venv.exists() or main_venv.is_symlink()):
        return None

    worktree_venv = worktree_path / ".venv"
    if worktree_venv.is_symlink() and worktree_venv.resolve() == main_venv.resolve():
        return worktree_venv

    if worktree_venv.is_symlink() or worktree_venv.exists():
        if worktree_venv.is_dir() and not worktree_venv.is_symlink():
            remove_tree_logged(
                worktree_venv,
                logger=logger,
                target_label="worktree venv directory",
            )
        else:
            worktree_venv.unlink()

    worktree_venv.symlink_to(main_venv, target_is_directory=main_venv.is_dir())
    return worktree_venv


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
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


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
                    from litehive.attention import append_attention_log

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


# === Worktree Rescue Operations ===


def collect_rescue_candidates(root: Path) -> list[RescueCandidate]:
    """Collect worktrees that need rescue (cherry-pick to main)."""
    return WorktreeService(root).collect_rescue_candidates()


def _collect_rescue_candidates(root: Path) -> list[RescueCandidate]:
    """Collect worktrees that need rescue (cherry-pick to main)."""
    candidates: list[RescueCandidate] = []
    for task in list_tasks(root, strict=False):
        if task.status != "flagged" or task.flag_reason != "merge_failed":
            continue
        worktree_rel = get_task_worktree_path(task)
        if not is_managed_worktree_path(root, worktree_rel):
            continue
        worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
        if worktree_path is None or worktree_rel is None:
            continue
        commit_shas = _worktree_commits_ahead_of_main(root, worktree_path) if worktree_path.exists() else []
        candidates.append(
            RescueCandidate(
                task_id=task.id,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                commit_shas=commit_shas,
            )
        )
    return sorted(candidates, key=lambda item: item.task_id)


def require_clean_main_checkout(root: Path) -> None:
    """Ensure main checkout is clean for rescue operations."""
    WorktreeService(root).require_clean_main_checkout()


def _require_clean_main_checkout(root: Path) -> None:
    """Ensure main checkout is clean for rescue operations."""
    branch = _git_stdout(root, "branch", "--show-current")
    if branch not in {"main", "master"}:
        raise GitError("worktree rescue --apply requires a clean checkout on branch 'main'")
    if has_non_litehive_changes(root):
        raise GitError("worktree rescue --apply requires a clean checkout on branch 'main'")


def apply_rescue_candidate(root: Path, candidate: RescueCandidate) -> RescueResult:
    """Apply rescue for a single candidate by cherry-picking commits to main."""
    return WorktreeService(root).apply_rescue_candidate(candidate)


def _apply_rescue_candidate(root: Path, candidate: RescueCandidate) -> RescueResult:
    """Apply rescue for a single candidate by cherry-picking commits to main."""
    task = get_task(root, candidate.task_id)
    if task is None:
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="missing_worktree",
            commit_shas=candidate.commit_shas,
            message="task record is missing",
        )
    if not candidate.worktree_path.exists():
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="missing_worktree",
            commit_shas=candidate.commit_shas,
            message="recorded worktree is missing",
        )
    if load_state(root).active_task_id == task.id:
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="active_task",
            commit_shas=candidate.commit_shas,
            message=(f"task {task.id} is still state.active_task_id; worktree rescue refuses to race with the runner"),
        )

    worktree_head = _git_stdout(candidate.worktree_path, "rev-parse", "HEAD")
    main_head = current_head(root)

    # Handle cases with no commits ahead
    if not candidate.commit_shas:
        if (
            worktree_head
            and main_head
            and worktree_head != main_head
            and _worktree_patch_already_on_main(root, worktree_head, main_head)
        ):
            try:
                _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
            except WorkspaceConflictError as exc:
                return RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="active_task",
                    commit_shas=[],
                    message=str(exc),
                )
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="already_landed",
                commit_shas=[],
                head_sha=main_head,
                message="worktree patch already landed on main",
            )
        if (
            worktree_head
            and main_head
            and worktree_head != main_head
            and _worktree_has_non_metadata_changes(root, candidate.worktree_path, task.id)
        ):
            try:
                _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
            except WorkspaceConflictError as exc:
                return RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="active_task",
                    commit_shas=[],
                    message=str(exc),
                )
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="already_landed",
                commit_shas=[],
                head_sha=main_head,
                message="worktree patch already landed on main",
            )
        try:
            _finalize_rescue(root, task, outcome="no-op", head_sha=main_head)
        except WorkspaceConflictError as exc:
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="active_task",
                commit_shas=[],
                message=str(exc),
            )
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="no_commits",
            commit_shas=[],
            head_sha=main_head,
            message="no worktree commits ahead of main",
        )

    # Check if patch already landed
    if worktree_head and main_head and _worktree_patch_already_on_main(root, worktree_head, main_head):
        try:
            _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
        except WorkspaceConflictError as exc:
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="active_task",
                commit_shas=candidate.commit_shas,
                message=str(exc),
            )
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="already_landed",
            commit_shas=candidate.commit_shas,
            head_sha=main_head,
            message="worktree patch already landed on main",
        )

    # Perform the actual rescue cherry-pick
    stashed_metadata = _stash_litehive_changes(root)
    for commit_sha in candidate.commit_shas:
        pick = subprocess.run(
            ["git", "cherry-pick", "--no-commit", commit_sha],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if pick.returncode != 0:
            conflicts = _git_lines(root, "diff", "--name-only", "--diff-filter=U")
            metadata_conflicts = [path for path in conflicts if _is_task_metadata_path(path, task.id)]
            if conflicts and len(metadata_conflicts) == len(conflicts):
                _resolve_metadata_conflicts(root, metadata_conflicts)
            else:
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                _restore_litehive_changes(root, stashed_metadata)
                save_task(root, task)
                _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
                return RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="manual_conflict",
                    commit_shas=candidate.commit_shas,
                    message=pick.stderr.strip() or "git cherry-pick failed",
                )

        _drop_task_metadata_changes(root, task.id)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--exit-code"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if staged.returncode == 0:
            continue
        if staged.returncode != 1:
            subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            _restore_litehive_changes(root, stashed_metadata)
            save_task(root, task)
            _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="manual_conflict",
                commit_shas=candidate.commit_shas,
                message="unable to inspect staged rescue changes",
            )

        commit = subprocess.run(
            ["git", "commit", "--reuse-message", commit_sha],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if commit.returncode != 0:
            subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            _restore_litehive_changes(root, stashed_metadata)
            save_task(root, task)
            _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="manual_conflict",
                commit_shas=candidate.commit_shas,
                message=commit.stderr.strip() or "git commit failed after rescue cherry-pick",
            )

    _restore_litehive_changes(root, stashed_metadata)
    head_sha = current_head(root)
    try:
        _finalize_rescue(root, task, outcome="rescued", head_sha=head_sha)
    except WorkspaceConflictError as exc:
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="active_task",
            commit_shas=candidate.commit_shas,
            head_sha=head_sha,
            message=str(exc),
        )
    return RescueResult(
        task_id=candidate.task_id,
        worktree_rel=candidate.worktree_rel,
        status="clean",
        commit_shas=candidate.commit_shas,
        head_sha=head_sha,
        message="rescued onto main",
    )


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
    fork_point = _git_stdout(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return []
    return sorted(set(_git_lines(worktree_path, "diff", "--name-only", fork_point, "HEAD")))


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


def _worktree_commits_ahead_of_main(root: Path, worktree_path: Path) -> list[str]:
    """Get commits in worktree that are ahead of main."""
    main_head = current_head(root) or "HEAD"
    fork_point = _git_stdout(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return []
    return _git_lines(worktree_path, "rev-list", "--reverse", f"{fork_point}..HEAD")


def _worktree_patch_already_on_main(root: Path, wt_head: str, main_head: str) -> bool:
    """Check if worktree patch is already on main."""
    cherry = subprocess.run(
        ["git", "cherry", main_head, wt_head],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if cherry.returncode != 0:
        return False
    lines = [line.strip() for line in cherry.stdout.splitlines() if line.strip()]
    return not lines or all(line.startswith("-") for line in lines)


def _is_task_metadata_path(path: str, task_id: str) -> bool:
    """Check if path is task metadata."""
    metadata_prefix = f".litehive/tasks/{task_id}-"
    return path.startswith(metadata_prefix)


def _resolve_metadata_conflicts(root: Path, paths: list[str]) -> None:
    """Resolve metadata conflicts by taking ours."""
    if not paths:
        return
    subprocess.run(
        ["git", "checkout", "--ours", "--", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "add", "--", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _drop_task_metadata_changes(root: Path, task_id: str) -> None:
    """Drop task metadata changes from staging."""
    changed_paths = _git_lines(root, "diff", "--cached", "--name-only")
    metadata_paths = [path for path in changed_paths if _is_task_metadata_path(path, task_id)]
    if not metadata_paths:
        return
    subprocess.run(
        ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", *metadata_paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _finalize_rescue(root: Path, task, *, outcome: str, head_sha: str | None) -> None:
    """Finalize rescue operation by updating task state."""
    from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    journal_message = "Worktree rescue found no commits ahead of main; cleared pending rescue state."
    if outcome == "rescued" and head_sha:
        journal_message = f"Worktree rescue applied onto main at {head_sha}."
    elif outcome == "already-landed" and head_sha:
        journal_message = f"Worktree rescue reconciled: patch already landed on main at {head_sha}."

    with workspace_lock(root):
        state = load_state(root)
        if state.active_task_id == task.id:
            raise WorkspaceConflictError(
                f"task {task.id} is still state.active_task_id; worktree rescue refuses to race with the runner"
            )
        ensure_future_task_mutation_allowed(root, [task.id], state=state)

        state.unmerged_worktrees = [entry for entry in state.unmerged_worktrees if entry.task_id != task.id]
        clear_task_worktree_path(task)
        if outcome in {"rescued", "already-landed", "no-op"}:
            task.status = "done"
            task.pipeline_status = "done"
            set_task_commit_sha(task, head_sha)
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=journal_message,
        )


def _clear_unmerged_worktree_state(root: Path, task_id: str) -> None:
    """Clear unmerged worktree state for task."""
    state = load_state(root)
    remaining = [entry for entry in state.unmerged_worktrees if entry.task_id != task_id]
    if len(remaining) == len(state.unmerged_worktrees):
        return
    state.unmerged_worktrees = remaining
    save_state(root, state)


def _ensure_unmerged_worktree_state(root: Path, task_id: str, worktree_rel: str) -> None:
    """Ensure unmerged worktree state is recorded."""
    state = load_state(root)
    for entry in state.unmerged_worktrees:
        if entry.task_id == task_id:
            return
    state.unmerged_worktrees.append(UnmergedWorktree(task_id=task_id, worktree_path=worktree_rel))
    save_state(root, state)


def _git_stdout(root: Path, *args: str) -> str | None:
    """Run git command and return stdout or None."""
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _git_lines(root: Path, *args: str) -> list[str]:
    """Run git command and return non-empty lines."""
    value = _git_stdout(root, *args)
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _stash_litehive_changes(root: Path) -> str | None:
    """Stash .litehive changes and return stash ref."""
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", ".litehive"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or not status.stdout.strip():
        return None
    before = _git_stdout(root, "rev-parse", "-q", "--verify", "refs/stash")
    subprocess.run(
        ["git", "stash", "push", "-u", "-m", "litehive-worktree-rescue", "--", ".litehive"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    after = _git_stdout(root, "rev-parse", "-q", "--verify", "refs/stash")
    if after and after != before:
        return after
    return None


def _restore_litehive_changes(root: Path, stash_ref: str | None) -> None:
    """Restore .litehive changes from stash."""
    if not stash_ref:
        return
    restored = subprocess.run(
        ["git", "stash", "pop", "--index", stash_ref],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if restored.returncode == 0:
        return
    subprocess.run(
        ["git", "stash", "apply", stash_ref],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "stash", "drop", stash_ref],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _worktree_has_non_metadata_changes(root: Path, worktree_path: Path, task_id: str) -> bool:
    """Check if worktree has non-metadata changes compared to main."""
    main_head = current_head(root) or "HEAD"
    fork_point = _git_stdout(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return False
    changed_paths = _git_lines(worktree_path, "diff", "--name-only", fork_point, "HEAD")
    return any(not _is_task_metadata_path(path, task_id) for path in changed_paths)
