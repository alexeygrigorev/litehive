"""Domain types for the worktree subsystem.

These types describe operator-visible state about Litehive-managed
git worktrees and the operations performed on them. The actual
behaviour (sync, cleanup, rescue) lives in ``litehive.worktree``;
this module is types-only so callers (lifecycle, CLI, tests) can
depend on the shapes without pulling in worktree mechanics or
git subprocess code.
"""

from dataclasses import dataclass
from pathlib import Path


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
        """True when ``litehive worktree clean`` may safely remove this entry: the task has reached a terminal status and is not the currently-active task."""
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
        """Carry the list of unresolved files alongside the formatted message so the merge-resolver lifecycle node can route them into a follow-up stage."""
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
