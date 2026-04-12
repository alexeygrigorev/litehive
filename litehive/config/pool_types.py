"""Shared dataclasses and utility types for the runtime module."""

from dataclasses import dataclass, field
from pathlib import Path

from litehive.config import ExecutionRetryPolicy
from litehive.models import TaskRecord
@dataclass(slots=True)
class RunResult:
    final_status: str
    steps_executed: int = 0
    last_verdict: str | None = None
from litehive.tasks.models import BlockedTask


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
    stop_on_execution_limit: bool = False
    quota_threshold: int | None = None
    budget_threshold: int | None = None
    pool_usage_cap: int | None = None
    pool_cost_cap: int | None = None
    engine_usage_caps: dict[str, int] = field(default_factory=dict)
    engine_budget_caps: dict[str, int] = field(default_factory=dict)
    engine_costs: dict[str, int] = field(default_factory=dict)
    stop_on_dirty_git: bool = False


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


@dataclass(frozen=True, slots=True)
class ResolvedExecutionRetryPolicy:
    selector: str
    policy: ExecutionRetryPolicy


@dataclass(slots=True)
class EngineBudgetLedger:
    pool_usage_cap: int | None = None
    pool_cost_cap: int | None = None
    engine_usage_caps: dict[str, int] = field(default_factory=dict)
    engine_budget_caps: dict[str, int] = field(default_factory=dict)
    engine_costs: dict[str, int] = field(default_factory=dict)
    total_usage: int = 0
    total_cost: int = 0
    usage_by_engine: dict[str, int] = field(default_factory=dict)
    cost_by_engine: dict[str, int] = field(default_factory=dict)

    def cost_for(self, engine_name: str) -> int:
        return self.engine_costs.get(engine_name, 1)

    def block_reason(self, engine_name: str) -> str | None:
        next_usage = self.total_usage + 1
        next_cost = self.total_cost + self.cost_for(engine_name)
        engine_usage = self.usage_by_engine.get(engine_name, 0) + 1
        engine_cost = self.cost_by_engine.get(engine_name, 0) + self.cost_for(engine_name)

        if self.pool_usage_cap is not None and next_usage > self.pool_usage_cap:
            return f"pool usage cap reached ({self.total_usage}/{self.pool_usage_cap})"
        if self.pool_cost_cap is not None and next_cost > self.pool_cost_cap:
            return f"pool cost cap reached ({self.total_cost}/{self.pool_cost_cap})"
        engine_usage_cap = self.engine_usage_caps.get(engine_name)
        if engine_usage_cap is not None and engine_usage > engine_usage_cap:
            return f"engine usage cap reached for `{engine_name}` ({self.usage_by_engine.get(engine_name, 0)}/{engine_usage_cap})"
        engine_budget_cap = self.engine_budget_caps.get(engine_name)
        if engine_budget_cap is not None and engine_cost > engine_budget_cap:
            return f"engine budget cap reached for `{engine_name}` ({self.cost_by_engine.get(engine_name, 0)}/{engine_budget_cap})"
        return None

    def record(self, engine_name: str) -> None:
        cost = self.cost_for(engine_name)
        self.total_usage += 1
        self.total_cost += cost
        self.usage_by_engine[engine_name] = self.usage_by_engine.get(engine_name, 0) + 1
        self.cost_by_engine[engine_name] = self.cost_by_engine.get(engine_name, 0) + cost

    def pool_stop_reason(self) -> str | None:
        if self.pool_usage_cap is not None and self.total_usage >= self.pool_usage_cap:
            return "pool_usage_cap_reached"
        if self.pool_cost_cap is not None and self.total_cost >= self.pool_cost_cap:
            return "pool_cost_cap_reached"
        return None


def _path_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
