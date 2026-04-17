"""Shared dataclasses and utility types for the runtime module."""

from dataclasses import dataclass, field
from pathlib import Path

from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import BlockedTask


@dataclass(slots=True)
class RunResult:
    final_status: str
    steps_executed: int = 0
    last_verdict: str | None = None


@dataclass(slots=True)
class ExecutionSummary:
    task: TaskRecord | None
    result: RunResult | None
    commit_sha: str | None = None


@dataclass(slots=True)
class TaskPoolRunSummary:
    executions: list[ExecutionSummary]
    stop_reason: str
    blocked: list[BlockedTask]


@dataclass(slots=True)
class SingleTaskRunSummary:
    execution: ExecutionSummary | None
    stop_reason: str
    blocked: list[BlockedTask]


@dataclass(slots=True)
class TaskPoolStopConditions:
    stop_on_failure: bool = False
    max_tasks: int | None = None
    stop_on_dirty_git: bool = False
    stop_on_attention: bool = False


@dataclass(slots=True)
class RollbackSummary:
    task: TaskRecord
    rollback_sha: str
    rolled_back_sha: str


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


def _path_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
