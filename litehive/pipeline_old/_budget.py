"""Budget tracking and execution-limit helpers."""

from litehive.config import LitehiveConfig

from ._types import EngineBudgetLedger, ExecutionSummary, TaskPoolStopConditions


def _execution_hit_limit(execution: ExecutionSummary) -> bool:
    return _execution_limit_kind(execution) is not None


def _execution_exhausted_limit_fallbacks(execution: ExecutionSummary) -> bool:
    task = execution.task
    if task is None:
        return False
    outcome = task.runtime.last_outcome
    if outcome.kind != "blocked":
        return False
    reason = outcome.reason.lower()
    return "exhausting engine fallbacks" in reason and _execution_limit_kind(execution) is not None


def _execution_limit_kind(execution: ExecutionSummary) -> str | None:
    task = execution.task
    if task is None:
        return None
    outcome = task.runtime.last_outcome
    if outcome.kind != "blocked":
        return None
    reason = outcome.reason.lower()
    if any(marker in reason for marker in ("budget", "credit", "insufficient funds")):
        return "budget"
    if any(
        marker in reason
        for marker in (
            "quota",
            "usage limit",
            "capacity limit",
            "rate limit",
            "too many requests",
        )
    ):
        return "quota"
    return None


def _count_execution_limits(executions: list[ExecutionSummary], *, kind: str) -> int:
    return sum(1 for execution in executions if _execution_limit_kind(execution) == kind)


def _limit_stop_condition_is_configured(
    conditions: TaskPoolStopConditions,
    kind: str | None,
) -> bool:
    if kind == "quota":
        return conditions.stop_on_execution_limit or conditions.quota_threshold is not None
    if kind == "budget":
        return conditions.stop_on_execution_limit or conditions.budget_threshold is not None
    return False


def _budget_ledger_from_config(config: LitehiveConfig) -> EngineBudgetLedger:
    return EngineBudgetLedger(
        pool_usage_cap=config.pool_usage_cap,
        pool_cost_cap=config.pool_cost_cap,
        engine_usage_caps=dict(config.engine_usage_caps),
        engine_budget_caps=dict(config.engine_budget_caps),
        engine_costs=dict(config.engine_costs),
    )


def _budget_ledger_from_conditions(conditions: TaskPoolStopConditions) -> EngineBudgetLedger:
    return EngineBudgetLedger(
        pool_usage_cap=conditions.pool_usage_cap,
        pool_cost_cap=conditions.pool_cost_cap,
        engine_usage_caps=dict(conditions.engine_usage_caps),
        engine_budget_caps=dict(conditions.engine_budget_caps),
        engine_costs=dict(conditions.engine_costs),
    )


def _engine_attempt_order(
    initial_engine_names: list[str], engine_preference: list[str]
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for engine_name in list(initial_engine_names) + engine_preference:
        if engine_name in seen:
            continue
        seen.add(engine_name)
        ordered.append(engine_name)
    return ordered
