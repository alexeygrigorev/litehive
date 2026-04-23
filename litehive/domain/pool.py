"""Shared dataclasses for pool/worktree operations."""

from dataclasses import dataclass, field

@dataclass(slots=True)
class DirtyWorktreeFinding:
    location_kind: str
    ownership: str
    dirty_paths: list[str] = field(default_factory=list)
    task_id: str | None = None
    worktree_path: str | None = None


@dataclass(slots=True)
class DirtyWorktreeGateReport:
    findings: list[DirtyWorktreeFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.findings

    @property
    def blocks_pool(self) -> bool:
        return any(
            finding.ownership in {"main-checkout", "ambiguous-ownership", "missing-recorded-worktree"}
            for finding in self.findings
        )
