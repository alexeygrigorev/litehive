"""
Records describing managed worktrees plus the merge-conflict exception.

Behaviour (sync, cleanup, rescue, inspection) lives in
``litehive.worktree``. Keeping the records here lets lifecycle code,
CLI rendering, and tests assert on the shapes without importing the
git/subprocess plumbing. ``WorktreeMergeConflict`` lives here too
because the lifecycle layer raises and routes on it.
"""

from dataclasses import dataclass
from pathlib import Path


_CLEANABLE_STATUSES = {"closed", "done"}


@dataclass(slots=True)
class ManagedWorktree:
    """
    Snapshot of one Litehive-managed task worktree.

    Built by the cleanup/listing flow with everything the CLI and
    pool gate need to render or decide on a worktree without
    re-asking git: identity (``task_id``, ``worktree_rel``,
    ``worktree_path``), the dirty count, and whether this is the
    currently-active task. Lives in ``domain`` so the listing CLI
    doesn't have to import the cleanup module.
    """

    task_id: str
    status: str
    worktree_rel: str
    worktree_path: Path
    change_count: int
    active: bool

    @property
    def cleanable(self) -> bool:
        """
        True when ``litehive worktree clean`` may safely remove this entry.

        A worktree is cleanable only when its task has reached a
        terminal status (``closed``/``done``) and is not the runner's
        currently-active task — removing the active worktree would
        yank the rug out from a live runner.
        """
        return self.status in _CLEANABLE_STATUSES and not self.active


@dataclass(slots=True)
class RescueCandidate:
    """
    Candidate identified by the rescue flow as needing operator triage.

    Surfaced when a flagged ``merge_failed`` task left commits on its
    worktree branch that never landed on main. The rescue CLI lists
    one ``RescueCandidate`` per such worktree and applies them via
    :func:`litehive.worktree.rescue.apply_rescue_candidate`.
    """

    task_id: str
    worktree_rel: str
    worktree_path: Path
    commit_shas: list[str]


@dataclass(slots=True)
class RescueResult:
    """
    Outcome record returned for one ``RescueCandidate``.

    ``status`` carries the high-level branch the rescue flow took
    (``clean``, ``already_landed``, ``no_commits``, ``manual_conflict``,
    ``missing_worktree``, ``active_task``); ``head_sha`` and
    ``message`` carry the operator-facing detail. The rescue CLI
    aggregates these and renders one summary table.
    """

    task_id: str
    worktree_rel: str
    status: str
    commit_shas: list[str]
    head_sha: str | None = None
    message: str | None = None


class WorktreeMergeConflict(Exception):
    """
    Raised when worktree sync ends with unresolved files in the index.

    The lifecycle layer catches this on the ``commit`` →
    ``merge_resolving`` boundary and stashes the conflict file list
    onto ``state.merge_context`` so the merge-resolver agent can act
    on the same paths the exception carried; without the typed
    exception, lifecycle would have to re-walk git status to find
    them.
    """

    def __init__(self, conflict_files: list[str]) -> None:
        """
        Carry the list of unresolved files alongside the formatted message.

        The lifecycle node reads ``conflict_files`` directly to seed
        the merge-resolver prompt; the message is what surfaces to
        operators via the failure summary if the resolver is never
        invoked.
        """
        super().__init__(f"{len(conflict_files)} unresolved file(s)")
        self.conflict_files = conflict_files


@dataclass(slots=True)
class WorktreeSyncResult:
    """
    Outcome record returned by lifecycle pre-exec worktree sync.

    ``changed=True`` tells the lifecycle layer the workspace moved
    (rebased onto a new main, or merged in origin) so the next stage
    sees fresh inputs; ``changed=False`` is the no-op fast path that
    keeps stable runs from looking unstable in operator output.
    """

    changed: bool
    worktree_path: Path | None = None


@dataclass(slots=True)
class TaskWorktreeInspection:
    """
    Per-task worktree snapshot rendered by status and diagnostics.

    Bundles "does the recorded worktree exist?", "what's dirty in
    it?", and "what's committed past main?" so a single read by the
    inspection helper feeds the operator's full picture without
    requiring three separate git roundtrips at the call site.
    """

    task_id: str
    worktree_rel: str | None
    worktree_path: Path | None
    exists: bool
    uncommitted: list[str]
    committed_ahead_of_main: list[str]
