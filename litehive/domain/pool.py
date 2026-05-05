"""
Dataclasses describing dirty-worktree findings for the pool gate.

The pool gate runs before any new task can claim the workspace and
needs a single, machine-friendly summary of "is anything dirty, and
who owns it?". Walking the worktree tree happens in
``litehive.worktree.inspection``; the records here are what gets
persisted to the pool-state SQLite row and rendered by status output.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class DirtyWorktreeFinding:
    """
    One dirty-state finding pinned to a workspace location.

    Records *where* the dirt is (main checkout vs. a task worktree),
    *who* it belongs to (a specific task, ambiguous, or unowned), and
    the file list. The pool gate aggregates a list of these into
    ``DirtyWorktreeGateReport`` and decides whether to proceed; status
    output uses them to tell the operator which task to clean up.
    """

    location_kind: str  # Where changes were found (main-checkout, task-worktree, etc.)
    ownership: str  # Who owns the changes (task-owned, orphaned, etc.)
    dirty_paths: list[str] = field(default_factory=list)  # Specific files with changes
    task_id: str | None = None  # Associated task if changes are task-owned
    worktree_path: str | None = None  # Path to the worktree with changes


@dataclass(slots=True)
class DirtyWorktreeGateReport:
    """
    Aggregate of every dirty-worktree finding for the pool gate.

    The gate writes one of these per scan; the runner refuses to claim
    a new task if ``blocks_pool`` is true, and status output reads the
    list to render the cleanup hints. Empty findings list = clean
    workspace = pool may proceed.
    """

    findings: list[DirtyWorktreeFinding] = field(default_factory=list)  # All uncommitted change locations found

    @property
    def is_clean(self) -> bool:
        """
        True when the workspace has no dirty-worktree findings.

        Cheap "may we proceed?" check used by status and the pool gate
        before doing any per-finding routing. ``blocks_pool`` is the
        stricter check that also tolerates task-owned dirt.
        """
        return not self.findings

    @property
    def blocks_pool(self) -> bool:
        """
        True when at least one finding is severe enough to halt the pool.

        Severe = dirt on the main checkout, ambiguous task ownership,
        or a registry-tracked worktree that doesn't actually exist on
        disk. Task-owned dirt is recorded for visibility but doesn't
        block on its own — the owning task can resume its own changes.
        """
        return any(
            finding.ownership in {"main-checkout", "ambiguous-ownership", "missing-recorded-worktree"}
            for finding in self.findings
        )
