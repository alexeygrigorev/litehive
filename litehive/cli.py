"""CLI entrypoint for litehive."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from litehive.config import (
    LitehiveConfig,
    VALID_TASK_ROUTING_KEYS,
    VALID_POOL_SELECTION_POLICIES,
    available_process_profiles,
    ensure_workspace,
    load_config,
    normalize_task_engine_routing,
)
from litehive.git_ops import GitError
from litehive.observability import render_task_summary
from litehive.runtime import (
    EngineBudgetLedger,
    TaskPoolStopConditions,
    recover_completed_task,
    resolve_engine_attempt_order,
    resolve_next_task,
    rollback_completed_task,
    run_task_pool,
)
from litehive.tasks import (
    VALID_HUMAN_CHECKPOINTS,
    VALID_TASK_PRIORITIES,
    WorkspaceConflictError,
    abandon_task,
    close_task,
    create_task,
    list_tasks,
    load_state,
    missing_acceptance_criteria_reason,
    move_queued_task,
    prioritize_queued_tasks,
    normalize_acceptance_criteria,
    normalize_human_checkpoints,
    plan_task_selections,
    requeue_task,
    resume_task,
    require_task,
    update_task_metadata,
)
from litehive.tui.app import LitehiveApp

ENGINE_CHOICES = ["codex", "opencode", "gemini", "copilot", "claude"]
TASK_TYPE_CHOICES = sorted(VALID_TASK_ROUTING_KEYS)


def _task_stage_outcomes(root: Path, task_id: str, slug: str) -> list[str]:
    reports_dir = root / ".litehive" / "tasks" / f"{task_id}-{slug}" / "reports"
    if not reports_dir.exists():
        return []

    outcomes: list[str] = []
    report_paths = sorted(
        reports_dir.glob("*.yaml"),
        key=lambda path: int(path.stem.rsplit("-", 1)[1]) if "-" in path.stem else 0,
    )
    for report_path in report_paths:
        report_data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        step = str(report_data.get("step") or "").strip()
        verdict = str(report_data.get("verdict") or "").strip()
        if step and verdict:
            outcomes.append(f"{step}={verdict}")
    return outcomes


def _pending_pool_tasks(root: Path) -> list[tuple[str, str, str, str, str]]:
    pending: list[tuple[str, str, str, str, str]] = []
    for task in list_tasks(root):
        if task.status in {"queued", "in_progress"} and task.pipeline_status != "done":
            pending.append((task.id, task.slug, task.title, task.status, task.pipeline_status))
    return pending


def _format_pool_task_report_line(
    root: Path,
    *,
    label: str,
    task_id: str,
    title: str,
    status: str,
    pipeline_status: str,
    slug: str | None = None,
) -> str:
    stage_outcomes = (
        _task_stage_outcomes(root, task_id, slug) if slug is not None else []
    )
    stage_outcomes_label = ", ".join(stage_outcomes) if stage_outcomes else "-"
    return (
        f"{label}: {task_id} {title} status={status} "
        f"pipeline_status={pipeline_status} stage_outcomes={stage_outcomes_label}"
    )


def _pool_stop_condition_label(stop_reason: str) -> str:
    labels = {
        "queue_exhausted": "queue exhausted",
        "blocked_tasks_remaining": "blocked tasks remaining",
        "stop_condition_reached": "custom stop condition reached",
        "max_tasks_reached": "max tasks reached",
        "failure_detected": "failure detected",
        "execution_limit_reached": "execution limit reached",
        "execution_limit_fallbacks_exhausted": "execution-limit fallbacks exhausted",
        "quota_threshold_reached": "quota threshold reached",
        "budget_threshold_reached": "budget threshold reached",
        "dirty_git_state": "dirty git state",
        "pool_usage_cap_reached": "pool usage cap reached",
        "pool_cost_cap_reached": "pool cost cap reached",
        "human_checkpoint_before_acceptance": "human checkpoint before acceptance",
        "human_checkpoint_before_commit": "human checkpoint before commit",
        "human_checkpoint_reached": "human checkpoint reached",
    }
    return labels.get(stop_reason, stop_reason.replace("_", " "))


def _print_pool_summary_report(
    root: Path,
    *,
    completed: list[tuple[str, str, str, str, str]],
    failed: list[tuple[str, str, str, str, str]],
    stop_reason: str,
    tasks_run: int | None = None,
) -> None:
    for line in _pool_summary_report_lines(
        root,
        completed=completed,
        failed=failed,
        stop_reason=stop_reason,
        tasks_run=tasks_run,
    ):
        print(line)


def _pool_summary_report_lines(
    root: Path,
    *,
    completed: list[tuple[str, str, str, str, str]],
    failed: list[tuple[str, str, str, str, str]],
    stop_reason: str,
    tasks_run: int | None = None,
) -> list[str]:
    remaining = _pending_pool_tasks(root)
    lines = [f"completed_tasks: {len(completed)}"]
    for task_id, slug, title, status, pipeline_status in completed:
        lines.append(
            _format_pool_task_report_line(
                root,
                label="completed",
                task_id=task_id,
                title=title,
                status=status,
                pipeline_status=pipeline_status,
                slug=slug,
            )
        )
    lines.append(f"failed_tasks: {len(failed)}")
    for task_id, slug, title, status, pipeline_status in failed:
        lines.append(
            _format_pool_task_report_line(
                root,
                label="failed",
                task_id=task_id,
                title=title,
                status=status,
                pipeline_status=pipeline_status,
                slug=slug,
            )
        )
    lines.append(f"skipped_tasks: {len(remaining)}")
    for task_id, slug, title, status, pipeline_status in remaining:
        lines.append(
            _format_pool_task_report_line(
                root,
                label="skipped",
                task_id=task_id,
                title=title,
                status=status,
                pipeline_status=pipeline_status,
                slug=slug,
            )
        )
    lines.append(f"remaining_tasks: {len(remaining)}")
    for task_id, slug, title, status, pipeline_status in remaining:
        lines.append(
            _format_pool_task_report_line(
                root,
                label="remaining",
                task_id=task_id,
                title=title,
                status=status,
                pipeline_status=pipeline_status,
                slug=slug,
            )
        )
    lines.append(f"tasks_run: {tasks_run if tasks_run is not None else len(completed) + len(failed)}")
    lines.append(f"stop_condition: {_pool_stop_condition_label(stop_reason)}")
    lines.append(f"stop_reason: {stop_reason}")
    return lines


def _write_pool_summary_report(
    root: Path,
    *,
    completed: list[tuple[str, str, str, str, str]],
    failed: list[tuple[str, str, str, str, str]],
    stop_reason: str,
    tasks_run: int | None = None,
) -> None:
    report_path = root / ".litehive" / "pool-summary.txt"
    report_lines = _pool_summary_report_lines(
        root,
        completed=completed,
        failed=failed,
        stop_reason=stop_reason,
        tasks_run=tasks_run,
    )
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def _format_engine_int_map(values: dict[str, int]) -> str:
    if not values:
        return "-"
    return ", ".join(f"{engine}={limit}" for engine, limit in sorted(values.items()))


def _determine_dry_run_stop_reason(
    blocked_reasons: list[str],
    *,
    stop_conditions: TaskPoolStopConditions,
) -> str:
    combined = " ".join(reason.lower() for reason in blocked_reasons)
    if "pool usage cap reached" in combined:
        return "pool_usage_cap_reached"
    if "pool cost cap reached" in combined:
        return "pool_cost_cap_reached"
    if stop_conditions.stop_on_execution_limit:
        return "execution_limit_reached"
    return "execution_limit_fallbacks_exhausted"


def _plan_pool_dry_run(
    root: Path,
    *,
    planned_tasks: list[object],
    blocked_count: int,
    config: LitehiveConfig,
    stop_conditions: TaskPoolStopConditions,
    engine_override: str | None,
) -> tuple[list[tuple[object, str, list[str]]], str]:
    from litehive.runtime import _git_worktree_is_dirty

    if stop_conditions.stop_on_dirty_git and _git_worktree_is_dirty(root):
        return [], "dirty_git_state"

    budget_ledger = EngineBudgetLedger(
        pool_usage_cap=stop_conditions.pool_usage_cap,
        pool_cost_cap=stop_conditions.pool_cost_cap,
        engine_usage_caps=dict(stop_conditions.engine_usage_caps),
        engine_budget_caps=dict(stop_conditions.engine_budget_caps),
        engine_costs=dict(stop_conditions.engine_costs),
    )
    runnable_tasks: list[tuple[object, str, list[str]]] = []

    for task in planned_tasks:
        if stop_conditions.max_tasks is not None and len(runnable_tasks) >= stop_conditions.max_tasks:
            return runnable_tasks, "max_tasks_reached"
        pool_stop_reason = budget_ledger.pool_stop_reason()
        if pool_stop_reason is not None:
            return runnable_tasks, pool_stop_reason

        engine_attempts = resolve_engine_attempt_order(
            task,
            config,
            engine_override=engine_override,
        )
        blocked_reasons: list[str] = []
        selected_engine: str | None = None
        for engine_name in engine_attempts:
            blocked_reason = budget_ledger.block_reason(engine_name)
            if blocked_reason is None:
                selected_engine = engine_name
                break
            blocked_reasons.append(blocked_reason)

        if selected_engine is None:
            return runnable_tasks, _determine_dry_run_stop_reason(
                blocked_reasons,
                stop_conditions=stop_conditions,
            )

        runnable_tasks.append((task, selected_engine, engine_attempts))
        budget_ledger.record(selected_engine)

    pool_stop_reason = budget_ledger.pool_stop_reason()
    if pool_stop_reason is not None:
        return runnable_tasks, pool_stop_reason
    if blocked_count:
        return runnable_tasks, "blocked_tasks_remaining"
    return runnable_tasks, "queue_exhausted"


def _print_pool_dry_run_plan(
    root: Path,
    *,
    planned_tasks: list[tuple[object, str, list[str]]],
    blocked: list[object],
    config: LitehiveConfig,
    stop_conditions: TaskPoolStopConditions,
    predicted_stop_reason: str,
) -> None:
    print("dry_run: true")
    print(f"selection_policy: {config.pool_selection_policy}")
    print(f"planned_tasks: {len(planned_tasks)}")
    for index, (task, selected_engine, engine_attempts) in enumerate(planned_tasks, start=1):
        checkpoints = ", ".join(task.human_checkpoints) if task.human_checkpoints else "-"
        print(
            f"would_run: {index}. {task.id} {task.title} "
            f"status={task.status} pipeline_status={task.pipeline_status} "
            f"engine={selected_engine} engine_attempts={', '.join(engine_attempts)} "
            f"human_checkpoints={checkpoints}"
        )
    print(f"blocked_tasks: {len(blocked)}")
    for blocked_task in blocked:
        print(
            f"blocked: {blocked_task.task_id} {blocked_task.title} "
            f"blocked_by={', '.join(blocked_task.blocked_by)}"
        )
    print(f"predicted_stop_condition: {_pool_stop_condition_label(predicted_stop_reason)}")
    print(f"predicted_stop_reason: {predicted_stop_reason}")
    print(f"stop_on_failure: {stop_conditions.stop_on_failure}")
    print(f"max_tasks: {stop_conditions.max_tasks}")
    print(f"stop_on_execution_limit: {stop_conditions.stop_on_execution_limit}")
    print(f"quota_threshold: {stop_conditions.quota_threshold}")
    print(f"budget_threshold: {stop_conditions.budget_threshold}")
    print(f"pool_usage_cap: {stop_conditions.pool_usage_cap}")
    print(f"pool_cost_cap: {stop_conditions.pool_cost_cap}")
    print(f"engine_usage_caps: {_format_engine_int_map(stop_conditions.engine_usage_caps)}")
    print(f"engine_budget_caps: {_format_engine_int_map(stop_conditions.engine_budget_caps)}")
    print(f"engine_costs: {_format_engine_int_map(stop_conditions.engine_costs)}")
    print(f"stop_on_dirty_git: {stop_conditions.stop_on_dirty_git}")


def _parse_dependency_ids(
    raw_values: list[str] | None,
    *,
    task_id: str | None = None,
    allow_clear: bool = False,
) -> list[str] | object:
    if not raw_values:
        return ...

    dependency_ids: list[str] = []
    for raw_value in raw_values:
        for item in raw_value.split(","):
            dependency_id = item.strip()
            if not dependency_id:
                raise ValueError("Dependency ids must not be empty")
            dependency_ids.append(dependency_id)

    if allow_clear and len(dependency_ids) == 1 and dependency_ids[0].lower() == "none":
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for dependency_id in dependency_ids:
        if dependency_id.lower() == "none":
            raise ValueError("'none' can only be used by itself")
        if task_id is not None and dependency_id == task_id:
            raise ValueError(f"Task {task_id} cannot depend on itself")
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        normalized.append(dependency_id)
    return normalized


def _parse_engine_int_map(raw_values: list[str] | None, *, option_name: str) -> dict[str, int]:
    if not raw_values:
        return {}

    mapping: dict[str, int] = {}
    for raw_value in raw_values:
        engine_name, separator, raw_int = raw_value.partition("=")
        if separator != "=":
            raise ValueError(f"{option_name} entries must use ENGINE=VALUE")
        engine_name = engine_name.strip()
        raw_int = raw_int.strip()
        if engine_name not in ENGINE_CHOICES:
            raise ValueError(f"{option_name} engine must be one of: {', '.join(ENGINE_CHOICES)}")
        try:
            value = int(raw_int)
        except ValueError as exc:
            raise ValueError(f"{option_name} value for {engine_name} must be an integer") from exc
        if value < 0:
            raise ValueError(f"{option_name} value for {engine_name} must be 0 or greater")
        mapping[engine_name] = value
    return mapping


def _parse_task_engine_routing(
    raw_values: list[str] | None,
    *,
    option_name: str,
) -> dict[str, list[str]]:
    if not raw_values:
        return {}

    routing: dict[str, list[str]] = {}
    for raw_value in raw_values:
        route_key, separator, raw_engines = raw_value.partition("=")
        if separator != "=":
            raise ValueError(f"{option_name} entries must use TASK_TYPE=ENGINE[,ENGINE...]")
        route_key = route_key.strip()
        if route_key not in TASK_TYPE_CHOICES:
            raise ValueError(f"{option_name} task type must be one of: {', '.join(TASK_TYPE_CHOICES)}")
        engines = [engine.strip() for engine in raw_engines.split(",") if engine.strip()]
        if not engines:
            raise ValueError(f"{option_name} route for {route_key} must include at least one engine")
        for engine_name in engines:
            if engine_name not in ENGINE_CHOICES:
                raise ValueError(f"{option_name} engine must be one of: {', '.join(ENGINE_CHOICES)}")
        routing[route_key] = engines
    return routing


def _parse_acceptance_criteria(
    raw_values: list[str] | None,
    *,
    allow_clear: bool = False,
) -> list[str] | object:
    if not raw_values:
        return ...

    normalized = normalize_acceptance_criteria(raw_values)
    if allow_clear and len(normalized) == 1 and normalized[0].lower() == "none":
        return []
    if any(item.lower() == "none" for item in normalized):
        raise ValueError("'none' can only be used by itself")
    if not normalized:
        raise ValueError("Acceptance criteria must not be empty")
    return normalized


def _parse_human_checkpoints(
    raw_values: list[str] | None,
    *,
    allow_clear: bool = False,
) -> list[str] | object:
    if not raw_values:
        return ...

    stripped = [item.strip() for item in raw_values if item.strip()]
    if allow_clear and len(stripped) == 1 and stripped[0].lower() == "none":
        return []
    if any(item.lower() == "none" for item in stripped):
        raise ValueError("'none' can only be used by itself")
    normalized = normalize_human_checkpoints(stripped)
    if not normalized:
        raise ValueError("Human checkpoints must not be empty")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="litehive")
    subparsers = parser.add_subparsers(dest="command")

    configure = subparsers.add_parser("configure", help="Initialize litehive workspace config")
    configure.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root where .litehive/ should be created",
    )
    configure.add_argument(
        "--default-engine",
        default="codex",
        help="Default engine adapter name",
    )
    configure.add_argument(
        "--process-profile",
        choices=available_process_profiles(),
        default="generic",
        help="Prompt/process overlay preset for workspace initialization",
    )
    configure.add_argument(
        "--default-retry-limit",
        type=int,
        default=3,
        help="Default retry limit for tasks without a task-specific override",
    )
    configure.add_argument(
        "--opencode-model",
        default="zai-coding-plan/glm-5.1",
        help="Default model identifier when using the opencode adapter",
    )
    configure.add_argument(
        "--gemini-model",
        default=None,
        help="Default model identifier when using the gemini adapter",
    )
    configure.add_argument(
        "--copilot-model",
        default=None,
        help="Default model identifier when using the copilot adapter",
    )
    configure.add_argument(
        "--claude-enabled",
        action="store_true",
        help="Enable the claude adapter (opt-in; disabled by default to protect quota)",
    )
    configure.add_argument(
        "--claude-model",
        default="claude-sonnet-4-20250514",
        help="Default model identifier when using the claude adapter",
    )
    configure.add_argument(
        "--claude-max-turns",
        type=int,
        default=30,
        help="Maximum conversation turns per claude invocation (guardrail against accidental quota burn)",
    )
    configure.add_argument(
        "--pool-usage-cap",
        type=int,
        default=None,
        help="Default pool behavior: stop before starting another engine invocation once this many invocations have run",
    )
    configure.add_argument(
        "--pool-cost-cap",
        type=int,
        default=None,
        help="Default pool behavior: stop before starting another engine invocation once this many cost units have been spent",
    )
    configure.add_argument(
        "--engine-usage-cap",
        action="append",
        default=None,
        help="Per-engine invocation cap as ENGINE=COUNT; repeat to set multiple engines",
    )
    configure.add_argument(
        "--engine-budget-cap",
        action="append",
        default=None,
        help="Per-engine budget cap in cost units as ENGINE=UNITS; repeat to set multiple engines",
    )
    configure.add_argument(
        "--engine-cost",
        action="append",
        default=None,
        help="Per-engine cost per invocation as ENGINE=UNITS; repeat to override defaults",
    )
    configure.add_argument(
        "--task-engine-route",
        action="append",
        default=None,
        help="Task routing override as TASK_TYPE=ENGINE[,ENGINE...]; repeat to override multiple task types",
    )
    configure.add_argument(
        "--pool-stop-on-failure",
        action="store_true",
        help="Default pool behavior: stop after the first task that does not finish successfully",
    )
    configure.add_argument(
        "--pool-max-tasks",
        type=int,
        default=None,
        help="Default pool behavior: stop after completing this many tasks",
    )
    configure.add_argument(
        "--pool-stop-on-limit",
        action="store_true",
        help="Default pool behavior: stop after a quota, budget, rate, credit, or similar execution limit is hit",
    )
    configure.add_argument(
        "--pool-quota-threshold",
        type=int,
        default=None,
        help="Default pool behavior: stop after this many quota-like limit outcomes in a run",
    )
    configure.add_argument(
        "--pool-budget-threshold",
        type=int,
        default=None,
        help="Default pool behavior: stop after this many budget-like limit outcomes in a run",
    )
    configure.add_argument(
        "--pool-stop-on-dirty-git",
        action="store_true",
        help="Default pool behavior: stop when the git worktree is dirty before starting another task",
    )
    configure.add_argument(
        "--pool-selection-policy",
        choices=sorted(VALID_POOL_SELECTION_POLICIES),
        default="dependency_aware",
        help="Default pool task ordering policy",
    )

    status = subparsers.add_parser("status", help="Show workspace status")
    status.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    queue = subparsers.add_parser("queue", help="Show the active task and queued order")
    queue.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    tasks = subparsers.add_parser("tasks", help="Open the task view")
    tasks.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    add = subparsers.add_parser("add", help="Create a queued task")
    add.add_argument("title", help="Task title")
    add.add_argument("--goal", default="", help="Task goal text")
    add.add_argument(
        "--acceptance-criteria",
        action="append",
        default=None,
        help="Add one acceptance criterion; repeat the flag for structured criteria",
    )
    add.add_argument(
        "--depends-on",
        action="append",
        help="Add prerequisite task ids; repeat the flag or use a comma-separated list",
    )
    add.add_argument(
        "--human-checkpoint",
        action="append",
        default=None,
        choices=sorted(VALID_HUMAN_CHECKPOINTS),
        help="Pause the task before this stage boundary; repeat for multiple checkpoints",
    )
    add.add_argument(
        "--task-type",
        choices=TASK_TYPE_CHOICES,
        help="Explicit routing class for this task",
    )
    add.add_argument(
        "--mode",
        choices=["implementation", "tasks"],
        help="Task creation mode; defaults to `tasks` when `--task-type` is set, otherwise `implementation`",
    )
    add.add_argument("--engine", choices=ENGINE_CHOICES, help="Preferred engine for the task")
    add.add_argument(
        "--retry-limit",
        type=int,
        help="Per-task retry limit override; omit to use the workspace default",
    )
    add.add_argument(
        "--no-auto-commit",
        action="store_true",
        help="Disable auto-commit for this task",
    )
    add.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    run = subparsers.add_parser("run", help="Drain the active and queued task pool")
    run.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned pool order, engines, and stop conditions without invoking any agents",
    )
    run.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        help="Override the engine for this run only",
    )
    run.add_argument(
        "--stop-on-failure",
        action="store_true",
        default=None,
        help="Stop the pool after the first task that does not finish successfully",
    )
    run.add_argument(
        "--max-tasks",
        type=int,
        help="Stop the pool after completing this many tasks",
    )
    run.add_argument(
        "--stop-on-limit",
        action="store_true",
        default=None,
        help="Stop the pool after a quota, budget, rate, credit, or similar execution limit is hit",
    )
    run.add_argument(
        "--quota-threshold",
        type=int,
        help="Stop the pool after this many quota-like limit outcomes in the current run",
    )
    run.add_argument(
        "--budget-threshold",
        type=int,
        help="Stop the pool after this many budget-like limit outcomes in the current run",
    )
    run.add_argument(
        "--pool-usage-cap",
        type=int,
        help="Stop before starting another engine invocation once this many invocations have run in the current pool",
    )
    run.add_argument(
        "--pool-cost-cap",
        type=int,
        help="Stop before starting another engine invocation once this many cost units have been spent in the current pool",
    )
    run.add_argument(
        "--engine-usage-cap",
        action="append",
        default=None,
        help="Per-engine invocation cap for this run as ENGINE=COUNT; repeat to set multiple engines",
    )
    run.add_argument(
        "--engine-budget-cap",
        action="append",
        default=None,
        help="Per-engine budget cap for this run in cost units as ENGINE=UNITS; repeat to set multiple engines",
    )
    run.add_argument(
        "--engine-cost",
        action="append",
        default=None,
        help="Per-engine cost for this run as ENGINE=UNITS; repeat to override defaults",
    )
    run.add_argument(
        "--stop-on-dirty-git",
        action="store_true",
        default=None,
        help="Stop the pool when the git worktree is dirty before starting another task",
    )

    rollback = subparsers.add_parser(
        "rollback", help="Revert a task checkpoint commit and requeue the task"
    )
    rollback.add_argument("task_id", help="Task id to roll back")
    rollback.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    recover = subparsers.add_parser(
        "recover", help="Requeue a completed task without reverting code"
    )
    recover.add_argument("task_id", help="Task id to recover")
    recover.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    move = subparsers.add_parser("move", help="Move a queued task to a 1-based position")
    move.add_argument("task_id", help="Queued task id to move")
    move.add_argument("position", type=int, help="Target queue position (1-based)")
    move.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    prioritize = subparsers.add_parser(
        "prioritize",
        help="Move multiple queued tasks to the front in the requested order",
    )
    prioritize.add_argument(
        "task_ids",
        nargs="+",
        help="Queued task ids to move to the front, in the requested order",
    )
    prioritize.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    promote = subparsers.add_parser("promote", help="Move a queued task to the front of the queue")
    promote.add_argument("task_id", help="Queued task id to promote")
    promote.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    requeue = subparsers.add_parser("requeue", help="Requeue a flagged, failed, or cancelled task")
    requeue.add_argument("task_id", help="Task id to requeue")
    requeue.add_argument(
        "--front",
        action="store_true",
        help="Insert the task at the front of the queue",
    )
    requeue.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    resume = subparsers.add_parser(
        "resume", help="Resume a flagged, failed, or cancelled task from its current stage"
    )
    resume.add_argument("task_id", help="Task id to resume")
    resume.add_argument(
        "--front",
        action="store_true",
        help="Insert the task at the front of the queue",
    )
    resume.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    abandon = subparsers.add_parser(
        "abandon", help="Cancel a flagged, failed, or cancelled task and remove it from the queue"
    )
    abandon.add_argument("task_id", help="Task id to abandon")
    abandon.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    close = subparsers.add_parser(
        "close",
        help="Close a task with an explicit non-implementation outcome (wont_do, deferred, duplicate)",
    )
    close.add_argument("task_id", help="Task id to close")
    close.add_argument(
        "--outcome",
        required=True,
        choices=["wont_do", "deferred", "duplicate"],
        help="Reason the task is being closed without implementation",
    )
    close.add_argument(
        "--reason",
        default=None,
        help="Optional free-text rationale recorded in the task journal",
    )
    close.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    update = subparsers.add_parser("update", help="Update task engine and metadata")
    update.add_argument("task_id", help="Task id to update")
    update.add_argument(
        "--engine",
        choices=[*ENGINE_CHOICES, "default"],
        help="Override task engine, or use 'default' to clear the override",
    )
    update.add_argument(
        "--retry-limit",
        type=str,
        help="Set task retry limit, or use 'default' to clear the override",
    )
    update.add_argument(
        "--priority",
        choices=sorted(VALID_TASK_PRIORITIES),
        help="Set task priority",
    )
    update.add_argument("--goal", help="Replace the task goal text")
    update.add_argument(
        "--depends-on",
        action="append",
        help="Replace task dependencies; repeat the flag or use a comma-separated list, or use 'none' to clear",
    )
    update.add_argument(
        "--acceptance-criteria",
        action="append",
        default=None,
        help="Replace acceptance criteria; repeat the flag or use 'none' to clear",
    )
    update.add_argument(
        "--human-checkpoint",
        action="append",
        default=None,
        choices=[*sorted(VALID_HUMAN_CHECKPOINTS), "none"],
        help="Replace human checkpoints; repeat the flag or use 'none' to clear",
    )
    update.add_argument(
        "--task-type",
        choices=[*TASK_TYPE_CHOICES, "default"],
        help="Override task routing class, or use 'default' to clear it",
    )
    update.add_argument(
        "--mode",
        choices=["tasks", "implementation"],
        help="Set task mode",
    )
    update.add_argument(
        "--auto-commit",
        dest="auto_commit",
        action="store_true",
        default=None,
        help="Enable auto-commit for this task",
    )
    update.add_argument(
        "--no-auto-commit",
        dest="auto_commit",
        action="store_false",
        help="Disable auto-commit for this task",
    )
    update.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    return parser


def _cmd_configure(args: argparse.Namespace) -> int:
    try:
        engine_usage_caps = _parse_engine_int_map(
            getattr(args, "engine_usage_cap", None),
            option_name="--engine-usage-cap",
        )
        engine_budget_caps = _parse_engine_int_map(
            getattr(args, "engine_budget_cap", None),
            option_name="--engine-budget-cap",
        )
        engine_costs = _parse_engine_int_map(
            getattr(args, "engine_cost", None),
            option_name="--engine-cost",
        )
        task_engine_routing = normalize_task_engine_routing(
            _parse_task_engine_routing(
                getattr(args, "task_engine_route", None),
                option_name="--task-engine-route",
            )
        )
    except ValueError as exc:
        print(f"configure failed: {exc}")
        return 1

    config = LitehiveConfig(
        default_engine=args.default_engine,
        process_profile=getattr(args, "process_profile", "generic"),
        default_retry_limit=getattr(args, "default_retry_limit", 3),
        opencode_model=args.opencode_model,
        gemini_model=args.gemini_model,
        copilot_model=getattr(args, "copilot_model", None),
        claude_enabled=getattr(args, "claude_enabled", False),
        claude_model=getattr(args, "claude_model", "claude-sonnet-4-20250514"),
        claude_max_turns=getattr(args, "claude_max_turns", 30),
        pool_usage_cap=getattr(args, "pool_usage_cap", None),
        pool_cost_cap=getattr(args, "pool_cost_cap", None),
        engine_usage_caps=engine_usage_caps,
        engine_budget_caps=engine_budget_caps,
        engine_costs=engine_costs or LitehiveConfig().engine_costs,
        pool_stop_on_failure=getattr(args, "pool_stop_on_failure", False),
        pool_max_tasks=getattr(args, "pool_max_tasks", None),
        pool_stop_on_execution_limit=getattr(args, "pool_stop_on_limit", False),
        pool_quota_threshold=getattr(args, "pool_quota_threshold", None),
        pool_budget_threshold=getattr(args, "pool_budget_threshold", None),
        pool_stop_on_dirty_git=getattr(args, "pool_stop_on_dirty_git", False),
        pool_selection_policy=getattr(args, "pool_selection_policy", "dependency_aware"),
        task_engine_routing=task_engine_routing,
    )
    ensure_workspace(args.workspace, config)
    print(f"Initialized litehive workspace in {args.workspace / '.litehive'}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.workspace)
    state = load_state(args.workspace)
    tasks = list_tasks(args.workspace)
    print(f"workspace: {args.workspace}")
    print(f"default_engine: {config.default_engine}")
    print(f"process_profile: {config.process_profile}")
    print(f"opencode_model: {config.opencode_model}")
    print(f"gemini_model: {config.gemini_model}")
    print(f"copilot_model: {config.copilot_model}")
    print(f"claude_enabled: {config.claude_enabled}")
    print(f"claude_model: {config.claude_model}")
    print(f"claude_max_turns: {config.claude_max_turns}")
    print(f"pool_usage_cap: {config.pool_usage_cap}")
    print(f"pool_cost_cap: {config.pool_cost_cap}")
    print(f"engine_usage_caps: {config.engine_usage_caps}")
    print(f"engine_budget_caps: {config.engine_budget_caps}")
    print(f"engine_costs: {config.engine_costs}")
    print(f"default_retry_limit: {config.default_retry_limit}")
    print(f"pool_stop_on_failure: {config.pool_stop_on_failure}")
    print(f"pool_max_tasks: {config.pool_max_tasks}")
    print(f"pool_stop_on_execution_limit: {config.pool_stop_on_execution_limit}")
    print(f"pool_quota_threshold: {config.pool_quota_threshold}")
    print(f"pool_budget_threshold: {config.pool_budget_threshold}")
    print(f"pool_stop_on_dirty_git: {config.pool_stop_on_dirty_git}")
    print(f"pool_selection_policy: {config.pool_selection_policy}")
    print(f"task_engine_routing: {config.task_engine_routing}")
    print(f"mode: {state.mode}")
    print(f"active_task_id: {state.active_task_id}")
    print(f"queued_tasks: {len(state.queue)}")
    print(f"pool_stop_reason: {state.pool_stop_reason}")
    if tasks:
        print()
        for task in tasks:
            for line in render_task_summary(task, active=task.id == state.active_task_id):
                print(line)
    return 0


def _task_engine_label(task_engine: str | None, default_engine: str) -> str:
    return task_engine or f"{default_engine} (default)"


def _task_dependencies_label(task_id: str, dependencies: list[str]) -> str:
    if not dependencies:
        return "-"
    return (
        ", ".join(dependency_id for dependency_id in dependencies if dependency_id != task_id)
        or "-"
    )


def _cmd_queue(args: argparse.Namespace) -> int:
    config = load_config(args.workspace)
    state = load_state(args.workspace)
    print(f"active_task_id: {state.active_task_id}")
    if state.active_task_id is not None:
        active_task = require_task(args.workspace, state.active_task_id)
        print(
            f"active: {active_task.id} [{active_task.status}/{active_task.pipeline_status}] "
            f"priority={active_task.priority} engine={_task_engine_label(active_task.engine, config.default_engine)} "
            f"title={active_task.title} depends_on={_task_dependencies_label(active_task.id, active_task.depends_on)}"
        )
    print(f"queue_length: {len(state.queue)}")
    for index, task_id in enumerate(state.queue, start=1):
        task = require_task(args.workspace, task_id)
        print(
            f"{index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={_task_engine_label(task.engine, config.default_engine)} "
            f"title={task.title} depends_on={_task_dependencies_label(task.id, task.depends_on)}"
        )
    return 0


def _launch_app(workspace: Path, default_mode: str) -> int:
    app = LitehiveApp(workspace=workspace, default_mode=default_mode)
    app.run()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    engine_override = getattr(args, "engine", None)
    config = load_config(args.workspace)
    try:
        engine_usage_caps = _cli_override_or_default(
            _parse_engine_int_map(
                getattr(args, "engine_usage_cap", None),
                option_name="--engine-usage-cap",
            )
            if getattr(args, "engine_usage_cap", None) is not None
            else None,
            config.engine_usage_caps,
        )
        engine_budget_caps = _cli_override_or_default(
            _parse_engine_int_map(
                getattr(args, "engine_budget_cap", None),
                option_name="--engine-budget-cap",
            )
            if getattr(args, "engine_budget_cap", None) is not None
            else None,
            config.engine_budget_caps,
        )
        engine_costs = _cli_override_or_default(
            _parse_engine_int_map(
                getattr(args, "engine_cost", None),
                option_name="--engine-cost",
            )
            if getattr(args, "engine_cost", None) is not None
            else None,
            config.engine_costs,
        )
    except ValueError as exc:
        print(f"run failed: {exc}")
        return 1
    stop_conditions = TaskPoolStopConditions(
        stop_on_failure=_cli_override_or_default(
            getattr(args, "stop_on_failure", None),
            config.pool_stop_on_failure,
        ),
        max_tasks=_cli_override_or_default(
            getattr(args, "max_tasks", None),
            config.pool_max_tasks,
        ),
        stop_on_execution_limit=_cli_override_or_default(
            getattr(args, "stop_on_limit", None),
            config.pool_stop_on_execution_limit,
        ),
        quota_threshold=_cli_override_or_default(
            getattr(args, "quota_threshold", None),
            config.pool_quota_threshold,
        ),
        budget_threshold=_cli_override_or_default(
            getattr(args, "budget_threshold", None),
            config.pool_budget_threshold,
        ),
        pool_usage_cap=_cli_override_or_default(
            getattr(args, "pool_usage_cap", None),
            config.pool_usage_cap,
        ),
        pool_cost_cap=_cli_override_or_default(
            getattr(args, "pool_cost_cap", None),
            config.pool_cost_cap,
        ),
        engine_usage_caps=engine_usage_caps,
        engine_budget_caps=engine_budget_caps,
        engine_costs=engine_costs,
        stop_on_dirty_git=_cli_override_or_default(
            getattr(args, "stop_on_dirty_git", None),
            config.pool_stop_on_dirty_git,
        ),
    )
    if args.dry_run:
        try:
            plan = plan_task_selections(args.workspace)
        except WorkspaceConflictError as exc:
            print(f"run failed: {exc}")
            return 1
        runnable_tasks, predicted_stop_reason = _plan_pool_dry_run(
            args.workspace,
            planned_tasks=plan.tasks,
            blocked_count=len(plan.blocked),
            config=config,
            stop_conditions=stop_conditions,
            engine_override=engine_override,
        )
        _print_pool_dry_run_plan(
            args.workspace,
            planned_tasks=runnable_tasks,
            blocked=plan.blocked,
            config=config,
            stop_conditions=stop_conditions,
            predicted_stop_reason=predicted_stop_reason,
        )
        return 0
    try:
        summary = run_task_pool(
            args.workspace,
            engine_override=engine_override,
            stop_conditions=stop_conditions,
        )
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1
    if not summary.executions:
        if summary.blocked:
            print("No runnable task.")
            for blocked in summary.blocked:
                print(
                    f"blocked: {blocked.task_id} {blocked.title} "
                    f"blocked_by={', '.join(blocked.blocked_by)}"
                )
            _write_pool_summary_report(
                args.workspace,
                completed=[],
                failed=[],
                stop_reason=summary.stop_reason,
                tasks_run=0,
            )
            _print_pool_summary_report(
                args.workspace,
                completed=[],
                failed=[],
                stop_reason=summary.stop_reason,
                tasks_run=0,
            )
            return 0
        if summary.stop_reason != "queue_exhausted":
            print("No task executed.")
            _write_pool_summary_report(
                args.workspace,
                completed=[],
                failed=[],
                stop_reason=summary.stop_reason,
                tasks_run=0,
            )
            _print_pool_summary_report(
                args.workspace,
                completed=[],
                failed=[],
                stop_reason=summary.stop_reason,
                tasks_run=0,
            )
            return 0
        print("No queued task.")
        _write_pool_summary_report(
            args.workspace,
            completed=[],
            failed=[],
            stop_reason=summary.stop_reason,
            tasks_run=0,
        )
        _print_pool_summary_report(
            args.workspace,
            completed=[],
            failed=[],
            stop_reason=summary.stop_reason,
            tasks_run=0,
        )
        return 0
    completed: list[tuple[str, str, str, str, str]] = []
    failed: list[tuple[str, str, str, str, str]] = []
    for execution in summary.executions:
        if execution.task is None:
            continue
        print(f"task: {execution.task.id} {execution.task.title}")
        if execution.result is not None:
            print(f"status: {execution.result.final_status}")
            print(f"steps: {execution.result.steps_executed}")
            print(f"last_verdict: {execution.result.last_verdict}")
            if execution.result.final_status == "done":
                completed.append(
                    (
                        execution.task.id,
                        execution.task.slug,
                        execution.task.title,
                        execution.task.status,
                        execution.task.pipeline_status,
                    )
                )
            elif execution.result.final_status != "paused":
                failed.append(
                    (
                        execution.task.id,
                        execution.task.slug,
                        execution.task.title,
                        execution.task.status,
                        execution.task.pipeline_status,
                    )
                )
        stage_outcomes = _task_stage_outcomes(
            args.workspace, execution.task.id, execution.task.slug
        )
        if stage_outcomes:
            print(f"stage_outcomes: {', '.join(stage_outcomes)}")
        if execution.commit_sha:
            print(f"commit: {execution.commit_sha}")
    for blocked in summary.blocked:
        print(
            f"blocked: {blocked.task_id} {blocked.title} blocked_by={', '.join(blocked.blocked_by)}"
        )
    _write_pool_summary_report(
        args.workspace,
        completed=completed,
        failed=failed,
        stop_reason=summary.stop_reason,
        tasks_run=len(summary.executions),
    )
    _print_pool_summary_report(
        args.workspace,
        completed=completed,
        failed=failed,
        stop_reason=summary.stop_reason,
        tasks_run=len(summary.executions),
    )
    return 0


def _cli_override_or_default(value: object, default: object) -> object:
    if value is None:
        return default
    if isinstance(default, bool) and value is False:
        return default
    return value


def _cmd_rollback(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        summary = rollback_completed_task(args.workspace, args.task_id)
    except (GitError, WorkspaceConflictError) as exc:
        print(f"rollback failed: {exc}")
        return 1

    print(f"task: {summary.task.id} {summary.task.title}")
    print(f"rollback_of: {summary.rolled_back_sha}")
    print(f"rollback_commit: {summary.rollback_sha}")
    print("status: queued")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print("recovery_policy: rollback reverted the checkpoint and requeued the task")
    print(f"next_commit_message: {summary.task.git.commit_message}")
    missing_criteria_reason = missing_acceptance_criteria_reason(summary.task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = recover_completed_task(args.workspace, args.task_id)
    except (GitError, WorkspaceConflictError) as exc:
        print(f"recover failed: {exc}")
        return 1

    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    print("recovery_policy: recover requeued the task without reverting workspace code")
    print(f"next_commit_message: {task.git.commit_message}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def _cmd_move(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        state = move_queued_task(args.workspace, args.task_id, args.position)
    except ValueError as exc:
        print(f"move failed: {exc}")
        return 1
    print(f"task_id: {args.task_id}")
    print(f"position: {state.queue.index(args.task_id) + 1}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = require_task(args.workspace, args.task_id)
        if task.status in {"flagged", "failed", "cancelled"}:
            task = resume_task(args.workspace, args.task_id, front=True)
            print(f"task: {task.id} {task.title}")
            print("status: queued")
            print(f"pipeline_status: {task.pipeline_status}")
            print("position: 1")
            return 0
        move_queued_task(args.workspace, args.task_id, 1)
    except ValueError as exc:
        print(f"promote failed: {exc}")
        return 1
    print(f"task_id: {args.task_id}")
    print("position: 1")
    return 0


def _cmd_prioritize(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        state = prioritize_queued_tasks(args.workspace, args.task_ids)
    except ValueError as exc:
        print(f"prioritize failed: {exc}")
        return 1
    print(f"tasks: {' '.join(args.task_ids)}")
    print(f"moved: {len(args.task_ids)}")
    print(f"front: {' '.join(state.queue[: len(args.task_ids)])}")
    print(f"queue_length: {len(state.queue)}")
    return 0


def _cmd_requeue_task(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = requeue_task(args.workspace, args.task_id, front=args.front)
    except ValueError as exc:
        print(f"requeue failed: {exc}")
        return 1
    state = load_state(args.workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


def _cmd_resume_task(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = resume_task(args.workspace, args.task_id, front=args.front)
    except ValueError as exc:
        print(f"resume failed: {exc}")
        return 1
    state = load_state(args.workspace)
    print(f"task: {task.id} {task.title}")
    print("status: queued")
    print(f"pipeline_status: {task.pipeline_status}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    print(f"position: {state.queue.index(task.id) + 1}")
    return 0


def _cmd_abandon_task(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = abandon_task(args.workspace, args.task_id)
    except ValueError as exc:
        print(f"abandon failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print("status: cancelled")
    print(f"pipeline_status: {task.pipeline_status}")
    return 0


def _cmd_close_task(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = close_task(args.workspace, args.task_id, outcome=args.outcome, reason=args.reason)
    except ValueError as exc:
        print(f"close failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print("status: cancelled")
    print(f"outcome: {task.runtime.last_outcome.reason_code}")
    print(f"pipeline_status: {task.pipeline_status}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        depends_on = _parse_dependency_ids(getattr(args, "depends_on", None))
        acceptance_criteria = _parse_acceptance_criteria(
            getattr(args, "acceptance_criteria", None)
        )
        human_checkpoints = _parse_human_checkpoints(getattr(args, "human_checkpoint", None))
        requested_task_type = getattr(args, "task_type", None)
        requested_mode = getattr(args, "mode", None)
        mode = requested_mode or ("tasks" if requested_task_type is not None else "implementation")
        task = create_task(
            args.workspace,
            title=args.title,
            depends_on=None if depends_on is ... else depends_on,
            mode=mode,
            goal=args.goal,
            acceptance_criteria=None if acceptance_criteria is ... else acceptance_criteria,
            human_checkpoints=None if human_checkpoints is ... else human_checkpoints,
            task_type=requested_task_type,
            engine=args.engine,
            retry_limit=getattr(args, "retry_limit", None),
            auto_commit=not args.no_auto_commit,
        )
    except ValueError as exc:
        print(f"add failed: {exc}")
        return 1
    print(
        f"Created task {task.id} in {args.workspace / '.litehive' / 'tasks' / (task.id + '-' + task.slug)}"
    )
    print(
        f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}"
    )
    print(f"mode: {task.mode}")
    print(
        "human_checkpoints: "
        + (", ".join(task.human_checkpoints) if task.human_checkpoints else "-")
    )
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {_task_dependencies_label(task.id, task.depends_on)}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    retry_limit_arg = getattr(args, "retry_limit", None)
    if (
        getattr(args, "depends_on", None) is None
        and getattr(args, "acceptance_criteria", None) is None
        and getattr(args, "human_checkpoint", None) is None
        and getattr(args, "task_type", None) is None
        and args.engine is None
        and retry_limit_arg is None
        and args.priority is None
        and args.goal is None
        and args.mode is None
        and args.auto_commit is None
    ):
        print("update failed: no changes requested")
        return 1
    try:
        depends_on = _parse_dependency_ids(
            getattr(args, "depends_on", None), task_id=args.task_id, allow_clear=True
        )
        acceptance_criteria = _parse_acceptance_criteria(
            getattr(args, "acceptance_criteria", None),
            allow_clear=True,
        )
        human_checkpoints = _parse_human_checkpoints(
            getattr(args, "human_checkpoint", None),
            allow_clear=True,
        )
        if retry_limit_arg is None:
            retry_limit: int | None | object = ...
        elif retry_limit_arg == "default":
            retry_limit = None
        else:
            retry_limit = int(retry_limit_arg)
        task = update_task_metadata(
            args.workspace,
            args.task_id,
            depends_on=depends_on,
            task_type=(
                None if getattr(args, "task_type", None) == "default" else getattr(args, "task_type", None)
            )
            if getattr(args, "task_type", None) is not None
            else ...,
            engine=(None if args.engine == "default" else args.engine)
            if args.engine is not None
            else ...,
            retry_limit=retry_limit,
            priority=args.priority if args.priority is not None else ...,
            goal=args.goal if args.goal is not None else ...,
            acceptance_criteria=acceptance_criteria,
            human_checkpoints=human_checkpoints,
            mode=args.mode if args.mode is not None else ...,
            auto_commit=args.auto_commit if args.auto_commit is not None else ...,
        )
    except ValueError as exc:
        print(f"update failed: {exc}")
        return 1
    config = load_config(args.workspace)
    print(f"task: {task.id} {task.title}")
    print(f"engine: {_task_engine_label(task.engine, config.default_engine)}")
    print(
        f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}"
    )
    print(f"priority: {task.priority}")
    print(f"mode: {task.mode}")
    print(f"auto_commit: {task.git.auto_commit}")
    print(
        "human_checkpoints: "
        + (", ".join(task.human_checkpoints) if task.human_checkpoints else "-")
    )
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {_task_dependencies_label(task.id, task.depends_on)}")
    print(f"goal: {task.goal}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "configure":
        return _cmd_configure(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "queue":
        return _cmd_queue(args)
    if args.command == "tasks":
        return _launch_app(args.workspace, default_mode="tasks")
    if args.command == "add":
        return _cmd_add(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "rollback":
        return _cmd_rollback(args)
    if args.command == "recover":
        return _cmd_recover(args)
    if args.command == "move":
        return _cmd_move(args)
    if args.command == "prioritize":
        return _cmd_prioritize(args)
    if args.command == "promote":
        return _cmd_promote(args)
    if args.command == "requeue":
        return _cmd_requeue_task(args)
    if args.command == "resume":
        return _cmd_resume_task(args)
    if args.command == "abandon":
        return _cmd_abandon_task(args)
    if args.command == "close":
        return _cmd_close_task(args)
    if args.command == "update":
        return _cmd_update(args)

    summary = run_next_task(Path.cwd())
    if summary.task is not None:
        if summary.result is not None:
            print(f"{summary.task.id}: {summary.result.final_status}")
        return 0
    return _launch_app(Path.cwd(), default_mode="implementation")


if __name__ == "__main__":
    raise SystemExit(main())
