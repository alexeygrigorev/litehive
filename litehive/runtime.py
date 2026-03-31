"""High-level runtime flow for executing queued tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

import yaml

from litehive.engines import EngineError
from litehive.config import LitehiveConfig, load_config, load_context
from litehive.git_ops import (
    GitError,
    checkpoint_message,
    commit_task,
    current_head,
    has_changes,
    is_git_repo,
    rollback_message,
    rollback_task,
    status_porcelain,
)
from litehive.models import StageReport, TaskRecord
from litehive.runner import RunResult, StageExecutor, TaskExecutionRunner
from litehive.subagents import SubagentManager, stage_prompt, stage_report_from_subagent
from litehive.tasks import (
    BlockedTask,
    active_task_markers,
    append_journal,
    enqueue_task,
    clear_active_task,
    dequeue_next_task,
    dequeue_next_task_selection,
    get_task,
    mark_engine_switch,
    mark_task_run_finished,
    mark_task_run_started,
    peek_next_task,
    peek_next_task_selection,
    restore_untouched_active_task,
    set_pool_stop_reason,
    save_task_runtime,
    save_task,
    workspace_mutation_guard,
    workspace_runner_guard,
    WorkspaceConflictError,
)


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


def resolve_next_task(root: Path) -> TaskRecord | None:
    root = root.resolve()
    return peek_next_task(root)


def run_next_task(root: Path) -> ExecutionSummary:
    root = root.resolve()
    task = dequeue_next_task(root)
    return run_task(root, task)


def run_task(
    root: Path,
    task: TaskRecord | None,
    *,
    engine_override: str | None = None,
    budget_ledger: EngineBudgetLedger | None = None,
) -> ExecutionSummary:
    root = root.resolve()
    if task is None:
        return ExecutionSummary(task=None, result=None)
    with workspace_runner_guard(root):
        markers = active_task_markers(root)
        if markers:
            active_ids = sorted(markers)
            if len(active_ids) > 1:
                details = "; ".join(
                    f"{task_id} ({', '.join(markers[task_id])})" for task_id in active_ids
                )
                raise WorkspaceConflictError(
                    f"workspace has multiple active tasks: {details}. Clear the stale active task state before running again."
                )
            active_id = active_ids[0]
            if active_id != task.id:
                raise WorkspaceConflictError(
                    f"task {task.id} cannot start because task {active_id} is already active in this workspace."
                )

        config = load_config(root)
        workspace_context = load_context(root)
        engine_name = resolve_engine_name(task, config, engine_override=engine_override)
        subagents = SubagentManager(root)

        append_journal(root, task, f"Execution started with engine `{engine_name}`.")
        mark_task_run_started(root, task)
        retry_limit, retry_source = resolve_task_retry_policy(task, config)

        runner = TaskExecutionRunner(
            root,
            build_executor(
                root,
                initial_engine_name=engine_name,
                workspace_context=workspace_context,
                subagents=subagents,
                config=config,
                config_auto_commit=config.auto_commit,
                budget_ledger=budget_ledger or _budget_ledger_from_config(config),
            ),
            max_retries=retry_limit,
            retry_source=retry_source,
        )
        result = runner.run(task)
        mark_task_run_finished(root, task, result.final_status)
        if result.final_status != "done":
            append_journal(root, task, f"Execution finished with status `{result.final_status}`.")
        clear_active_task(root)

        return ExecutionSummary(task=task, result=result, commit_sha=task.git.commit_sha)


def run_task_pool(
    root: Path,
    *,
    engine_override: str | None = None,
    stop_conditions: TaskPoolStopConditions | None = None,
    stop_when: Callable[[list[ExecutionSummary]], bool] | None = None,
) -> TaskPoolRunSummary:
    root = root.resolve()
    with workspace_runner_guard(root):
        executions: list[ExecutionSummary] = []
        conditions = stop_conditions or TaskPoolStopConditions()
        budget_ledger = _budget_ledger_from_conditions(conditions)
        set_pool_stop_reason(root, None)

        while True:
            stop_reason = _pool_stop_reason(root, executions, conditions, budget_ledger=budget_ledger)
            if stop_reason is not None:
                return _finalize_pool_run(
                    root, executions=executions, stop_reason=stop_reason, blocked=[]
                )
            if stop_when is not None and stop_when(executions):
                return _finalize_pool_run(
                    root,
                    executions=executions,
                    stop_reason="stop_condition_reached",
                    blocked=[],
                )
            execution, blocked = run_next_task_with_override(
                root,
                engine_override=engine_override,
                budget_ledger=budget_ledger,
            )
            if execution.task is None:
                stop_reason = "blocked_tasks_remaining" if blocked else "queue_exhausted"
                return _finalize_pool_run(
                    root, executions=executions, stop_reason=stop_reason, blocked=blocked
                )
            executions.append(execution)


def run_next_task_with_override(
    root: Path,
    *,
    engine_override: str | None = None,
    budget_ledger: EngineBudgetLedger | None = None,
) -> tuple[ExecutionSummary, list[BlockedTask]]:
    root = root.resolve()
    selection = dequeue_next_task_selection(root)
    return (
        run_task(
            root,
            selection.task,
            engine_override=engine_override,
            budget_ledger=budget_ledger,
        ),
        selection.blocked,
    )


def _pool_stop_reason(
    root: Path,
    executions: list[ExecutionSummary],
    conditions: TaskPoolStopConditions,
    *,
    budget_ledger: EngineBudgetLedger | None = None,
) -> str | None:
    if conditions.stop_on_dirty_git and _git_worktree_is_dirty(root):
        return "dirty_git_state"
    if conditions.max_tasks is not None and len(executions) >= conditions.max_tasks:
        return "max_tasks_reached"
    if budget_ledger is not None:
        budget_stop_reason = budget_ledger.pool_stop_reason()
        if budget_stop_reason is not None:
            return budget_stop_reason
    if not executions:
        return None

    latest = executions[-1]
    latest_limit_kind = _execution_limit_kind(latest)
    final_status = latest.result.final_status if latest.result is not None else None
    if conditions.stop_on_failure and final_status is not None and final_status != "done":
        return "failure_detected"
    if conditions.stop_on_execution_limit and _execution_hit_limit(latest):
        return "execution_limit_reached"
    if (
        latest_limit_kind == "quota"
        and conditions.quota_threshold is not None
        and _count_execution_limits(executions, kind="quota") >= conditions.quota_threshold
    ):
        return "quota_threshold_reached"
    if (
        latest_limit_kind == "budget"
        and conditions.budget_threshold is not None
        and _count_execution_limits(executions, kind="budget") >= conditions.budget_threshold
    ):
        return "budget_threshold_reached"
    if _execution_exhausted_limit_fallbacks(latest) and not _limit_stop_condition_is_configured(
        conditions,
        latest_limit_kind,
    ):
        return "execution_limit_fallbacks_exhausted"
    return None


def _git_worktree_is_dirty(root: Path) -> bool:
    return is_git_repo(root) and has_changes(root)


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


def _finalize_pool_run(
    root: Path,
    *,
    executions: list[ExecutionSummary],
    stop_reason: str,
    blocked: list[BlockedTask],
) -> TaskPoolRunSummary:
    restore_untouched_active_task(root)
    set_pool_stop_reason(root, stop_reason)
    if stop_reason == "execution_limit_fallbacks_exhausted" and executions:
        latest = executions[-1]
        if latest.task is not None:
            outcome_reason = latest.task.runtime.last_outcome.reason or "engine fallbacks exhausted"
            append_journal(root, latest.task, f"Pool stopped: {stop_reason}. {outcome_reason}")
    return TaskPoolRunSummary(executions=executions, stop_reason=stop_reason, blocked=blocked)


def rollback_completed_task(root: Path, task_id: str) -> RollbackSummary:
    root = root.resolve()
    with workspace_mutation_guard(root):
        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        _require_completed_task(task, action="rollback")

        rollback = rollback_task(root, task)
        attempt = task.git.checkpoint_attempts
        task.status = "queued"
        task.pipeline_status = "implementing"
        task.runtime.last_outcome.kind = None
        task.runtime.last_outcome.stage = None
        task.runtime.last_outcome.reason_code = None
        task.runtime.last_outcome.reason = ""
        task.runtime.last_outcome.retry_count = 0
        task.runtime.last_outcome.retry_limit = 0
        task.runtime.last_outcome.retry_source = "global"
        task.runtime.last_outcome.recorded_at = None
        task.runtime.retry_count = 0
        task.runtime.retry_limit = 0
        task.runtime.retry_source = "global"
        task.git.commit_sha = None
        task.git.rolled_back_checkpoint_attempt = attempt
        append_journal(
            root,
            task,
            (
                "Checkpoint rollback requested.\n"
                f"- rolled_back: `{rollback.rolled_back_sha}`\n"
                f"- recovery_stage: `implementing`"
            ),
        )
        save_task(root, task)

        rollback_checkpoint = commit_task(root, rollback_message(task, attempt))
        if rollback_checkpoint is None:
            raise GitError("git rollback commit failed")

        enqueue_task(root, task.id)
        return RollbackSummary(
            task=task,
            rollback_sha=rollback_checkpoint.commit_sha,
            rolled_back_sha=rollback.rolled_back_sha,
        )


def recover_completed_task(root: Path, task_id: str) -> TaskRecord:
    root = root.resolve()
    with workspace_mutation_guard(root):
        task = get_task(root, task_id)
        if task is None:
            raise GitError(f"Task {task_id} not found")
        _require_completed_task(task, action="recover")

        task.status = "queued"
        task.pipeline_status = "implementing"
        task.runtime.last_outcome.kind = None
        task.runtime.last_outcome.stage = None
        task.runtime.last_outcome.reason_code = None
        task.runtime.last_outcome.reason = ""
        task.runtime.last_outcome.retry_count = 0
        task.runtime.last_outcome.retry_limit = 0
        task.runtime.last_outcome.retry_source = "global"
        task.runtime.last_outcome.recorded_at = None
        task.runtime.retry_count = 0
        task.runtime.retry_limit = 0
        task.runtime.retry_source = "global"
        task.git.commit_sha = None
        append_journal(root, task, "Task recovered for another implementation pass.")
        save_task(root, task)
        enqueue_task(root, task.id)
        return task


def _role_for_step(step: str) -> str:
    return {
        "grooming": "pm",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "pm",
    }.get(step, "swe")


def resolve_engine_name(
    task: TaskRecord,
    config: LitehiveConfig,
    *,
    engine_override: str | None = None,
) -> str:
    if engine_override is not None:
        _require_claude_enabled(engine_override, config)
        return engine_override
    if task.engine is not None:
        _require_claude_enabled(task.engine, config)
        return task.engine
    _require_claude_enabled(config.default_engine, config)
    return config.default_engine


def _require_claude_enabled(engine_name: str, config: LitehiveConfig) -> None:
    if engine_name == "claude" and not config.claude_enabled:
        raise EngineError(
            "Claude engine is opt-in. Set claude_enabled: true in config.yaml to enable it."
        )


def resolve_task_retry_policy(task: TaskRecord, config: LitehiveConfig) -> tuple[int, str]:
    if task.retry_policy.max_retries is not None:
        return task.retry_policy.max_retries, "task"
    return config.default_retry_limit, "global"


def _require_completed_task(task: TaskRecord, action: str) -> None:
    if task.status != "done" or task.pipeline_status != "done":
        raise GitError(f"Task {task.id} is not completed; cannot {action}")


def build_executor(
    root: Path,
    *,
    initial_engine_name: str,
    workspace_context: str,
    subagents: SubagentManager,
    config: LitehiveConfig,
    config_auto_commit: bool,
    budget_ledger: EngineBudgetLedger,
) -> StageExecutor:
    def executor(current_task: TaskRecord, step: str) -> StageReport:
        if step == "commit_to_git":
            return _commit_to_git_report(
                root,
                current_task,
                auto_commit_enabled=config_auto_commit and current_task.git.auto_commit,
            )

        prompt = stage_prompt(
            current_task,
            step,
            workspace_context=workspace_context,
            process_profile=config.process_profile,
        )
        fallback_events: list[str] = []
        engines = _engine_attempt_order(initial_engine_name, config.engine_fallbacks)
        limit_trigger_reason: str | None = None

        for index, engine_name in enumerate(engines):
            budget_reason = budget_ledger.block_reason(engine_name)
            if budget_reason is not None:
                if index + 1 < len(engines):
                    next_engine = engines[index + 1]
                    event = (
                        f"Stage `{step}` switched from `{engine_name}` to `{next_engine}` "
                        f"after {budget_reason}."
                    )
                    fallback_events.append(event)
                    append_journal(root, current_task, event)
                    mark_engine_switch(
                        root,
                        current_task,
                        step=step,
                        from_engine=engine_name,
                        to_engine=next_engine,
                        reason=budget_reason,
                    )
                    continue
                report = StageReport(
                    task_id=current_task.id,
                    step=step,  # type: ignore[arg-type]
                    verdict="blocked",
                    summary=f"{step} blocked: {budget_reason}",
                    feedback="\n\n".join(fallback_events).strip(),
                    warnings=[*fallback_events, budget_reason],
                )
                return report

            budget_ledger.record(engine_name)
            result = subagents.run(
                current_task,
                role=_role_for_step(step),
                engine_name=engine_name,
                prompt=prompt,
                model=_model_for_engine(config, engine_name),
                max_turns=(config.claude_max_turns if engine_name == "claude" else None),
            )
            is_limit_failure = (
                result.failure is not None and result.failure.kind == "execution_limit"
            )
            if is_limit_failure and limit_trigger_reason is None:
                limit_trigger_reason = result.failure.reason
            is_unavailable_fallback = (
                limit_trigger_reason is not None
                and result.failure is not None
                and result.failure.kind == "engine_error"
            )
            if (is_limit_failure or is_unavailable_fallback) and index + 1 < len(engines):
                next_engine = engines[index + 1]
                event = (
                    f"Stage `{step}` switched from `{engine_name}` to `{next_engine}` "
                    f"after {result.failure.reason}."
                )
                fallback_events.append(event)
                append_journal(root, current_task, event)
                mark_engine_switch(
                    root,
                    current_task,
                    step=step,
                    from_engine=engine_name,
                    to_engine=next_engine,
                    reason=result.failure.reason,
                )
                continue

            report = stage_report_from_subagent(current_task, step, result)
            if fallback_events:
                report.warnings = [*fallback_events, *report.warnings]
                report.feedback = "\n\n".join([*fallback_events, report.feedback]).strip()
            if limit_trigger_reason is not None and (is_limit_failure or is_unavailable_fallback):
                if (
                    is_unavailable_fallback
                    and result.failure is not None
                    and result.failure.reason != limit_trigger_reason
                ):
                    report.summary = (
                        f"{step} blocked after exhausting engine fallbacks following {limit_trigger_reason}: "
                        f"{result.failure.reason}"
                    )
                else:
                    report.summary = (
                        f"{step} blocked after exhausting engine fallbacks: {result.failure.reason}"
                    )
                report.feedback = "\n\n".join([*fallback_events, result.transcript]).strip()
                if not report.warnings or report.warnings[-1] != result.failure.reason:
                    report.warnings.append(result.failure.reason)
            return report

        raise RuntimeError("Engine attempt order must include at least one engine")

    return executor


def _model_for_engine(config: LitehiveConfig, engine_name: str) -> str | None:
    if engine_name == "opencode":
        return config.opencode_model
    if engine_name == "gemini":
        return config.gemini_model
    if engine_name == "copilot":
        return config.copilot_model
    if engine_name == "claude":
        return config.claude_model
    return None


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
    initial_engine_name: str, engine_fallbacks: dict[str, list[str]]
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for engine_name in [initial_engine_name, *engine_fallbacks.get(initial_engine_name, [])]:
        if engine_name in seen:
            continue
        seen.add(engine_name)
        ordered.append(engine_name)
    return ordered


def _commit_to_git_report(
    root: Path, task: TaskRecord, *, auto_commit_enabled: bool
) -> StageReport:
    if not auto_commit_enabled:
        task.status = "done"
        task.pipeline_status = "done"
        save_task(root, task)
        append_journal(root, task, "CommitToGit skipped: auto-commit disabled.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped because auto-commit is disabled",
            warnings=["auto-commit disabled"],
        )

    if not is_git_repo(root):
        append_journal(root, task, "CommitToGit failed: workspace is not a git repository.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: workspace is not a git repository",
            warnings=["workspace is not a git repository"],
        )

    try:
        dirty_entries = status_porcelain(root)
    except GitError as exc:
        append_journal(root, task, f"CommitToGit failed: {exc}")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary=f"CommitToGit failed: {exc}",
            warnings=[str(exc)],
        )

    if not dirty_entries:
        append_journal(root, task, "CommitToGit failed: repository has no changes to commit.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: repository has no changes to commit",
            warnings=["no changes to commit"],
        )

    unexpected_dirty_paths = _unexpected_dirty_paths(root, task, dirty_entries)
    if unexpected_dirty_paths:
        message = "repository has unrelated changes: " + ", ".join(
            f"`{path}`" for path in unexpected_dirty_paths
        )
        append_journal(root, task, f"CommitToGit failed: {message}.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary=f"CommitToGit failed: {message}",
            warnings=["repository contains unrelated uncommitted changes"],
        )

    try:
        base_sha = current_head(root)
        attempt = task.git.checkpoint_attempts + 1
        message = checkpoint_message(task, attempt=attempt)
        previous_base_sha = task.git.checkpoint_base_sha
        previous_attempts = task.git.checkpoint_attempts
        previous_rollback_attempt = task.git.rolled_back_checkpoint_attempt
        previous_status = task.status
        previous_pipeline_status = task.pipeline_status
        task.git.commit_sha = None
        task.git.checkpoint_base_sha = base_sha
        task.git.checkpoint_attempts = attempt
        task.git.rolled_back_checkpoint_attempt = None
        task.status = "done"
        task.pipeline_status = "done"
        append_journal(
            root,
            task,
            (
                "CommitToGit requested.\n"
                f"- base: `{base_sha or 'initial commit'}`\n"
                f"- message: `{message}`"
            ),
        )
        save_task(root, task)
        checkpoint = commit_task(root, message)
        if checkpoint is None:
            raise GitError("git commit prerequisites were not met")
    except GitError as exc:
        task.git.checkpoint_base_sha = previous_base_sha
        task.git.checkpoint_attempts = previous_attempts
        task.git.rolled_back_checkpoint_attempt = previous_rollback_attempt
        task.git.commit_sha = None
        task.status = previous_status
        task.pipeline_status = previous_pipeline_status
        save_task(root, task)
        append_journal(root, task, f"CommitToGit failed: {exc}")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary=f"CommitToGit failed: {exc}",
            warnings=[str(exc)],
        )

    task.git.commit_sha = checkpoint.commit_sha
    save_task_runtime(root, task)
    return StageReport(
        task_id=task.id,
        step="commit_to_git",
        verdict="pass",
        summary="CommitToGit created the final checkpoint commit",
    )


def _unexpected_dirty_paths(root: Path, task: TaskRecord, dirty_entries: list[str]) -> list[str]:
    unexpected: list[str] = []
    for entry in dirty_entries:
        path = _status_entry_path(entry)
        if path is None or _is_allowed_commit_path(root, task, path):
            continue
        unexpected.append(path)
    return unexpected


def _allowed_commit_paths(root: Path, task: TaskRecord) -> set[PurePosixPath]:
    allowed = {
        PurePosixPath(".litehive") / "config.yaml",
        PurePosixPath(".litehive") / "context.md",
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
    }
    reports_dir = root / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    for report_path in reports_dir.glob("*.yaml"):
        report_data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        for changed in report_data.get("files_changed") or []:
            normalized = str(changed).strip()
            if normalized:
                allowed.add(PurePosixPath(normalized))
    return allowed


def _is_allowed_commit_path(root: Path, task: TaskRecord, path: str) -> bool:
    candidate = PurePosixPath(path)
    for allowed in _allowed_commit_paths(root, task):
        if candidate == allowed or allowed in candidate.parents:
            return True
    return False


def _status_entry_path(entry: str) -> str | None:
    if len(entry) < 4:
        return None
    path = entry[3:]
    if " -> " in path:
        return path.split(" -> ", 1)[1].strip()
    return path.strip() or None
