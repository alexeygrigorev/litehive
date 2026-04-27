"""Shared dataclasses for pool/worktree operations."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class DirtyWorktreeFinding:
    """Record of uncommitted changes found in a specific workspace location.

    Created by workspace health checks and worktree gate validation.
    Used by PipelineRunner and pool management to decide whether it's
    safe to proceed with task execution or pool operations.
    """

    location_kind: str  # Where changes were found (main-checkout, task-worktree, etc.)
    ownership: str  # Who owns the changes (task-owned, orphaned, etc.)
    dirty_paths: list[str] = field(default_factory=list)  # Specific files with changes
    task_id: str | None = None  # Associated task if changes are task-owned
    worktree_path: str | None = None  # Path to the worktree with changes


@dataclass(slots=True)
class DirtyWorktreeGateReport:
    """Aggregate report of all dirty worktree findings in the workspace.

    Collects all uncommitted changes found during workspace health validation.
    Used by gates to determine whether pool operations should proceed and
    by operators to understand what needs cleanup before safe execution.
    """

    findings: list[DirtyWorktreeFinding] = field(default_factory=list)  # All uncommitted change locations found

    @property
    def is_clean(self) -> bool:
        return not self.findings

    @property
    def blocks_pool(self) -> bool:
        return any(
            finding.ownership in {"main-checkout", "ambiguous-ownership", "missing-recorded-worktree"}
            for finding in self.findings
        )
