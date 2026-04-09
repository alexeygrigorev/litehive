from litehive.config import ensure_workspace, load_config
from litehive.runtime import TaskPoolStopConditions, drain_task_pool, run_single_task
from litehive.tasks import (
    WorkspaceConflictError,
    peek_next_task_selection,
    plan_task_selections,
)

from litehive.cli._pool import (
    _pool_summary_report_data,
    _pool_task_report_entry,
    _print_pool_summary_report,
    _task_stage_outcomes,
    _write_pool_summary_report,
)
from litehive.cli._dry_run import (
    _plan_pool_dry_run,
    _plan_single_task_dry_run,
    _print_pool_dry_run_plan,
)
from litehive.cli._display import _cli_override_or_default
from litehive.cli._parse import _parse_engine_int_map


def _cmd_run(args):
    ensure_workspace(args.workspace)
    engine_override = getattr(args, "engine", None)
    model_override = getattr(args, "model", None)
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
    if bool(getattr(args, "parallel", False)):
        return _cmd_run_parallel(
            args,
            config=config,
            engine_override=engine_override,
            model_override=model_override,
        )
    if bool(getattr(args, "drain", False)):
        return _cmd_run_drain(
            args,
            config=config,
            stop_conditions=stop_conditions,
            engine_override=engine_override,
            model_override=model_override,
        )
    return _cmd_run_single(
        args,
        config=config,
        stop_conditions=stop_conditions,
        engine_override=engine_override,
        model_override=model_override,
    )


def _cmd_run_drain(
    args,
    *,
    config,
    stop_conditions,
    engine_override,
    model_override,
):
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
            model_override=model_override,
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
        summary = drain_task_pool(
            args.workspace,
            engine_override=engine_override,
            model_override=model_override,
            stop_conditions=stop_conditions,
        )
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1
    if not summary.executions:
        report = _pool_summary_report_data(
            args.workspace,
            completed=[],
            flagged=[],
            stop_reason=summary.stop_reason,
            tasks_run=0,
        )
        if summary.blocked:
            print("No runnable task.")
            for blocked in summary.blocked:
                print(
                    f"blocked: {blocked.task_id} {blocked.title} "
                    f"blocked_by={', '.join(blocked.blocked_by)}"
                )
            _write_pool_summary_report(root=args.workspace, report=report)
            _print_pool_summary_report(report=report)
            return 0
        if summary.stop_reason != "queue_exhausted":
            print("No task executed.")
            _write_pool_summary_report(root=args.workspace, report=report)
            _print_pool_summary_report(report=report)
            return 0
        print("No queued task.")
        _write_pool_summary_report(root=args.workspace, report=report)
        _print_pool_summary_report(report=report)
        return 0
    completed = []
    flagged = []
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
                    _pool_task_report_entry(
                        args.workspace,
                        task_id=execution.task.id,
                        title=execution.task.title,
                        status=execution.task.status,
                        pipeline_status=execution.task.pipeline_status,
                        slug=execution.task.slug,
                    )
                )
            elif execution.result.final_status not in {"paused", "queued", "interrupted"}:
                flagged.append(
                    _pool_task_report_entry(
                        args.workspace,
                        task_id=execution.task.id,
                        title=execution.task.title,
                        status=execution.task.status,
                        pipeline_status=execution.task.pipeline_status,
                        slug=execution.task.slug,
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
    report = _pool_summary_report_data(
        args.workspace,
        completed=completed,
        flagged=flagged,
        stop_reason=summary.stop_reason,
        tasks_run=len(summary.executions),
    )
    _write_pool_summary_report(root=args.workspace, report=report)
    _print_pool_summary_report(report=report)
    return 0


def _cmd_run_single(
    args,
    *,
    config,
    stop_conditions,
    engine_override,
    model_override,
):
    if args.dry_run:
        try:
            selection = peek_next_task_selection(args.workspace)
        except WorkspaceConflictError as exc:
            print(f"run failed: {exc}")
            return 1
        planned_tasks = [selection.task] if selection.task is not None else []
        runnable_tasks, predicted_stop_reason = _plan_single_task_dry_run(
            args.workspace,
            planned_tasks=planned_tasks,
            blocked_count=len(selection.blocked),
            config=config,
            stop_conditions=stop_conditions,
            engine_override=engine_override,
            model_override=model_override,
        )
        _print_pool_dry_run_plan(
            args.workspace,
            planned_tasks=runnable_tasks,
            blocked=selection.blocked,
            config=config,
            stop_conditions=stop_conditions,
            predicted_stop_reason=predicted_stop_reason,
        )
        return 0

    try:
        summary = run_single_task(
            args.workspace,
            engine_override=engine_override,
            model_override=model_override,
            stop_conditions=stop_conditions,
        )
    except WorkspaceConflictError as exc:
        print(f"run failed: {exc}")
        return 1
    stop_reason = summary.stop_reason
    execution = summary.execution
    blocked = summary.blocked
    if stop_reason is not None and (execution is None or execution.task is None):
        report = _pool_summary_report_data(
            args.workspace,
            completed=[],
            flagged=[],
            stop_reason=stop_reason,
            tasks_run=0,
        )
        if blocked:
            print("No runnable task.")
            for blocked_task in blocked:
                print(
                    f"blocked: {blocked_task.task_id} {blocked_task.title} "
                    f"blocked_by={', '.join(blocked_task.blocked_by)}"
                )
            _write_pool_summary_report(root=args.workspace, report=report)
            _print_pool_summary_report(report=report)
            return 0
        if stop_reason == "queue_exhausted":
            print("No queued task.")
        else:
            print("No task executed.")
        _write_pool_summary_report(root=args.workspace, report=report)
        _print_pool_summary_report(report=report)
        return 0

    completed = []
    flagged = []
    task = execution.task
    result = execution.result
    print(f"task: {task.id} {task.title}")
    if result is not None:
        print(f"status: {result.final_status}")
        print(f"steps: {result.steps_executed}")
        print(f"last_verdict: {result.last_verdict}")
        if result.final_status == "done":
            completed.append(
                _pool_task_report_entry(
                    args.workspace,
                    task_id=task.id,
                    title=task.title,
                    status=task.status,
                    pipeline_status=task.pipeline_status,
                    slug=task.slug,
                )
            )
        elif result.final_status not in {"paused", "queued", "interrupted"}:
            flagged.append(
                _pool_task_report_entry(
                    args.workspace,
                    task_id=task.id,
                    title=task.title,
                    status=task.status,
                    pipeline_status=task.pipeline_status,
                    slug=task.slug,
                )
            )
    stage_outcomes = _task_stage_outcomes(args.workspace, task.id, task.slug)
    if stage_outcomes:
        print(f"stage_outcomes: {', '.join(stage_outcomes)}")
    if execution.commit_sha:
        print(f"commit: {execution.commit_sha}")

    report = _pool_summary_report_data(
        args.workspace,
        completed=completed,
        flagged=flagged,
        stop_reason=stop_reason,
        tasks_run=1,
    )
    _write_pool_summary_report(root=args.workspace, report=report)
    _print_pool_summary_report(report=report)
    return 0


def _cmd_run_parallel(
    args,
    *,
    config,
    engine_override,
    model_override,
):
    from litehive.pipeline._parallel import run_parallel_tasks

    if config.parallel_capacity < 2:
        print("parallel_capacity must be >= 2 in config for parallel mode; using 2")

    try:
        summary = run_parallel_tasks(
            args.workspace,
            engine_override=engine_override,
            model_override=model_override,
            capacity=config.parallel_capacity if config.parallel_capacity >= 2 else None,
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
        elif summary.stop_reason == "queue_exhausted":
            print("No queued task.")
        else:
            print("No task executed.")
        return 0

    print(f"parallel run: {len(summary.executions)} task(s)")
    for execution in summary.executions:
        if execution.task is None:
            continue
        status = execution.result.final_status if execution.result else "error"
        print(f"  {execution.task.id} {execution.task.title} -> {status}")

    if summary.integration_results:
        print("integration:")
        for ir in summary.integration_results:
            status_label = "ok" if ir.success else "FAILED"
            conflict_label = " (conflict resolved by agent)" if ir.agent_resolved else ""
            if ir.conflict and not ir.success:
                conflict_label = " (UNRESOLVED CONFLICT)"
            sha_label = f" {ir.commit_sha[:8]}" if ir.commit_sha else ""
            print(f"  {ir.task_id}: {status_label}{conflict_label}{sha_label}")

    print(f"stop_reason: {summary.stop_reason}")
    return 0
