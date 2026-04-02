"""CLI entrypoint for litehive."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

import yaml

from litehive.config import (
    LitehiveConfig,
    SubagentResourceLimitsConfig,
    VALID_TASK_ROUTING_KEYS,
    VALID_POOL_SELECTION_POLICIES,
    available_process_profiles,
    config_path,
    context_path,
    ensure_workspace,
    format_external_engine_sandbox,
    format_runner_hooks,
    format_subagent_resource_limits,
    load_config,
    normalize_task_engine_routing,
    render_context_template,
)
from litehive.git_ops import GitError, checkpoint_message
from litehive.observability import render_task_summary
from litehive.models import utcnow
from litehive.runtime import (
    EngineBudgetLedger,
    TaskPoolStopConditions,
    drain_task_pool,
    recover_completed_task,
    resolve_model,
    resolve_engine_attempt_order,
    run_single_task,
    rollback_completed_task,
)
from litehive.engines import (
    ENGINE_CHOICES,
    get_engine,
)
from litehive.subagents import intake_prompt
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
    missing_acceptance_criteria_cli_warning,
    move_queued_task,
    prioritize_queued_tasks,
    normalize_acceptance_criteria,
    normalize_human_checkpoints,
    plan_task_selections,
    peek_next_task_selection,
    repair_workspace_state,
    requeue_task,
    recover_stale_runner_state,
    resume_task,
    require_task,
    set_pool_stop_reason,
    update_task_metadata,
)
from litehive.tui.app import LitehiveApp

TASK_TYPE_CHOICES = sorted(VALID_TASK_ROUTING_KEYS)


def _fallback_intake_title(brain_dump: str) -> str:
    first_nonempty = next((line.strip() for line in brain_dump.splitlines() if line.strip()), "")
    if not first_nonempty:
        return "Unstructured Intake"
    compact = " ".join(first_nonempty.split())
    return compact[:77] + "..." if len(compact) > 80 else compact


def _fallback_intake_goal(brain_dump: str) -> str:
    lines = [" ".join(line.split()) for line in brain_dump.splitlines() if line.strip()]
    if not lines:
        return "Capture the original intake and prepare it for PM grooming."
    summary = " ".join(lines)
    summary = summary[:237] + "..." if len(summary) > 240 else summary
    return summary


def _link_intake_brief_to_source(brief_file: Path) -> None:
    if not brief_file.exists():
        return
    content = brief_file.read_text(encoding="utf-8")
    stub_pattern = "### Intake Notes\n- Capture the core brain dump or link to the source.\n\n_TBD_"
    replacement = (
        "### Intake Notes\n"
        "- Original dump: [intake.md](intake.md)\n"
        "- Treat `intake.md` as the authoritative source for the raw specification.\n"
    )
    if stub_pattern in content:
        brief_file.write_text(content.replace(stub_pattern, replacement), encoding="utf-8")


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


def _pool_task_report_entry(
    root: Path,
    *,
    task_id: str,
    title: str,
    status: str,
    pipeline_status: str,
    slug: str | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
    follow_up_task_id: str | None = None,
) -> dict[str, object]:
    stage_outcomes = _task_stage_outcomes(root, task_id, slug) if slug is not None else []
    return {
        "task_id": task_id,
        "title": title,
        "final_task_status": status,
        "pipeline_status": pipeline_status,
        "stage_outcomes": stage_outcomes,
        "reason_code": reason_code,
        "reason": reason,
        "follow_up_task_id": follow_up_task_id,
    }


def _pending_pool_tasks(root: Path) -> list[dict[str, object]]:
    pending: list[dict[str, object]] = []
    for task in list_tasks(root):
        if task.status in {"queued", "in_progress"} and task.pipeline_status != "done":
            pending.append(
                _pool_task_report_entry(
                    root,
                    task_id=task.id,
                    title=task.title,
                    status=task.status,
                    pipeline_status=task.pipeline_status,
                    slug=task.slug,
                )
            )
    return pending


def _resumable_pool_tasks(root: Path) -> list[dict[str, object]]:
    resumable: list[dict[str, object]] = []
    for task in list_tasks(root):
        if task.status != "interrupted" or task.pipeline_status == "done":
            continue
        resumable.append(
            _pool_task_report_entry(
                root,
                task_id=task.id,
                title=task.title,
                status=task.status,
                pipeline_status=task.pipeline_status,
                slug=task.slug,
                reason_code=task.runtime.last_outcome.reason_code,
                reason=task.runtime.last_outcome.reason,
                follow_up_task_id=task.runtime.last_outcome.follow_up_task_id,
            )
        )
    return resumable


def _closed_pool_tasks(root: Path) -> list[dict[str, object]]:
    closed: list[dict[str, object]] = []
    for task in list_tasks(root):
        if task.status not in {"wont_do", "deferred", "duplicate"}:
            continue
        closed.append(
            _pool_task_report_entry(
                root,
                task_id=task.id,
                title=task.title,
                status=task.status,
                pipeline_status=task.pipeline_status,
                slug=task.slug,
                reason_code=task.runtime.last_outcome.reason_code,
                reason=task.runtime.last_outcome.reason,
                follow_up_task_id=task.runtime.last_outcome.follow_up_task_id,
            )
        )
    return closed


def _format_pool_task_report_line(
    *,
    label: str,
    entry: dict[str, object],
) -> str:
    stage_outcomes = [str(item) for item in entry.get("stage_outcomes", [])]
    stage_outcomes_label = ", ".join(stage_outcomes) if stage_outcomes else "-"
    line = (
        f"{label}: {entry['task_id']} {entry['title']} status={entry['final_task_status']} "
        f"pipeline_status={entry['pipeline_status']} stage_outcomes={stage_outcomes_label}"
    )
    reason_code = entry.get("reason_code")
    if reason_code:
        line += f" reason_code={reason_code}"
    reason = entry.get("reason")
    if reason:
        line += f" reason={reason}"
    follow_up_task_id = entry.get("follow_up_task_id")
    if follow_up_task_id:
        line += f" follow_up_task={follow_up_task_id}"
    return line


def _pool_stop_condition_label(stop_reason: str) -> str:
    labels = {
        "single_task_complete": "single task complete",
        "queue_exhausted": "queue exhausted",
        "task_requeued": "task requeued for another pass",
        "task_interrupted": "task interrupted and awaiting resume",
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


def _pool_no_useful_progress_report(stop_reason: str) -> tuple[str | None, str | None]:
    reports = {
        "blocked_tasks_remaining": (
            "no_useful_progress",
            "Pool stopped with no useful progress because no runnable task remained.",
        ),
        "task_requeued": (
            "no_useful_progress",
            "Pool stopped with no useful progress because the active task was requeued for another pass.",
        ),
        "task_interrupted": (
            "no_useful_progress",
            "Pool stopped with no useful progress because the active task was interrupted and must be resumed.",
        ),
        "execution_limit_fallbacks_exhausted": (
            "no_useful_progress",
            "Pool stopped with no useful progress because engine fallbacks were exhausted.",
        ),
    }
    return reports.get(stop_reason, (None, None))


def _print_pool_summary_report(
    *,
    report: dict[str, object],
) -> None:
    for line in _pool_summary_report_lines(report=report):
        print(line)


def _pool_summary_report_data(
    root: Path,
    *,
    completed: list[dict[str, object]],
    flagged: list[dict[str, object]],
    stop_reason: str,
    tasks_run: int | None = None,
) -> dict[str, object]:
    remaining = _pending_pool_tasks(root)
    resumable = _resumable_pool_tasks(root)
    closed = _closed_pool_tasks(root)
    progress_status, summary = _pool_no_useful_progress_report(stop_reason)
    return {
        "created_at": utcnow(),
        "summary": summary,
        "progress_status": progress_status,
        "stop_condition": _pool_stop_condition_label(stop_reason),
        "stop_reason": stop_reason,
        "tasks_run": tasks_run if tasks_run is not None else len(completed) + len(flagged),
        "completed_count": len(completed),
        "completed": completed,
        "flagged_count": len(flagged),
        "flagged": flagged,
        "resumable_count": len(resumable),
        "resumable": resumable,
        "closed_count": len(closed),
        "closed": closed,
        "skipped_count": len(remaining),
        "skipped": remaining,
        "remaining_count": len(remaining),
        "remaining": remaining,
    }


def _pool_summary_report_lines(
    *,
    report: dict[str, object],
) -> list[str]:
    report = _ensure_pool_summary_report_fields(report)
    completed = [entry for entry in report["completed"] if isinstance(entry, dict)]
    flagged = [entry for entry in report["flagged"] if isinstance(entry, dict)]
    resumable = [entry for entry in report["resumable"] if isinstance(entry, dict)]
    closed = [entry for entry in report["closed"] if isinstance(entry, dict)]
    skipped = [entry for entry in report["skipped"] if isinstance(entry, dict)]
    remaining = [entry for entry in report["remaining"] if isinstance(entry, dict)]
    lines = [f"completed_tasks: {report['completed_count']}"]
    for entry in completed:
        lines.append(
            _format_pool_task_report_line(
                label="completed",
                entry=entry,
            )
        )
    lines.append(f"flagged_tasks: {report['flagged_count']}")
    for entry in flagged:
        lines.append(
            _format_pool_task_report_line(
                label="flagged",
                entry=entry,
            )
        )
    lines.append(f"resumable_tasks: {report['resumable_count']}")
    for entry in resumable:
        lines.append(
            _format_pool_task_report_line(
                label="resumable",
                entry=entry,
            )
        )
    lines.append(f"closed_tasks: {report['closed_count']}")
    for entry in closed:
        lines.append(
            _format_pool_task_report_line(
                label="closed",
                entry=entry,
            )
        )
    lines.append(f"skipped_tasks: {report['skipped_count']}")
    for entry in skipped:
        lines.append(
            _format_pool_task_report_line(
                label="skipped",
                entry=entry,
            )
        )
    lines.append(f"remaining_tasks: {report['remaining_count']}")
    for entry in remaining:
        lines.append(
            _format_pool_task_report_line(
                label="remaining",
                entry=entry,
            )
        )
    lines.append(f"tasks_run: {report['tasks_run']}")
    if report.get("progress_status") is not None:
        lines.append(f"progress_status: {report['progress_status']}")
    if report.get("summary") is not None:
        lines.append(f"summary: {report['summary']}")
    lines.append(f"stop_condition: {report['stop_condition']}")
    lines.append(f"stop_reason: {report['stop_reason']}")
    return lines


def _ensure_pool_summary_report_fields(report: dict[str, object]) -> dict[str, object]:
    progress_status = report.get("progress_status")
    summary = report.get("summary")
    if progress_status is not None or summary is not None:
        return report
    stop_reason = str(report.get("stop_reason", ""))
    derived_progress_status, derived_summary = _pool_no_useful_progress_report(stop_reason)
    if derived_progress_status is None and derived_summary is None:
        return report
    enriched = dict(report)
    enriched["progress_status"] = derived_progress_status
    enriched["summary"] = derived_summary
    return enriched


def _write_pool_summary_report(
    *,
    root: Path,
    report: dict[str, object],
) -> None:
    report = _ensure_pool_summary_report_fields(report)
    report_path = root / ".litehive" / "pool-summary.txt"
    report_lines = _pool_summary_report_lines(report=report)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    _write_durable_pool_run_report(root, report=report)


def _write_durable_pool_run_report(root: Path, *, report: dict[str, object]) -> None:
    reports_dir = root / ".litehive" / "logs" / "pool-runs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = str(report["created_at"]).replace(":", "-").replace("+00:00", "Z")
    report_path = reports_dir / f"{timestamp}.yaml"
    suffix = 1
    while report_path.exists():
        suffix += 1
        report_path = reports_dir / f"{timestamp}-{suffix:02d}.yaml"
    report_path.write_text(yaml.safe_dump(report, sort_keys=False), encoding="utf-8")


def _format_engine_int_map(values: dict[str, int]) -> str:
    if not values:
        return "-"
    return ", ".join(f"{engine}={limit}" for engine, limit in sorted(values.items()))


def _format_execution_retry_policies(config: LitehiveConfig) -> str:
    if not config.execution_retry_policies:
        return "-"
    parts: list[str] = []
    for selector, policy in sorted(config.execution_retry_policies.items()):
        retry_on = ",".join(policy.retry_on) if policy.retry_on else "-"
        parts.append(
            f"{selector}=retries:{policy.max_retries} "
            f"backoff:{policy.backoff_seconds:.2f}s "
            f"multiplier:{policy.backoff_multiplier:.2f} "
            f"retry_on:{retry_on}"
        )
    return "; ".join(parts)


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
    model_override: str | None,
) -> tuple[list[tuple[object, str, list[str], str | None]], str]:
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
    runnable_tasks: list[tuple[object, str, list[str], str | None]] = []

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

        selected_model = resolve_model(
            task,
            config,
            engine_name=selected_engine,
            model_override=model_override,
        )
        runnable_tasks.append((task, selected_engine, engine_attempts, selected_model))
        budget_ledger.record(selected_engine)

    pool_stop_reason = budget_ledger.pool_stop_reason()
    if pool_stop_reason is not None:
        return runnable_tasks, pool_stop_reason
    if blocked_count:
        return runnable_tasks, "blocked_tasks_remaining"
    return runnable_tasks, "queue_exhausted"


def _plan_single_task_dry_run(
    root: Path,
    *,
    planned_tasks: list[object],
    blocked_count: int,
    config: LitehiveConfig,
    stop_conditions: TaskPoolStopConditions,
    engine_override: str | None,
    model_override: str | None,
) -> tuple[list[tuple[object, str, list[str], str | None]], str]:
    from litehive.runtime import _git_worktree_is_dirty

    if stop_conditions.stop_on_dirty_git and _git_worktree_is_dirty(root):
        return [], "dirty_git_state"
    if not planned_tasks:
        if blocked_count:
            return [], "blocked_tasks_remaining"
        return [], "queue_exhausted"

    budget_ledger = _budget_ledger_from_stop_conditions(stop_conditions)
    task = planned_tasks[0]
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
        return [], _determine_dry_run_stop_reason(
            blocked_reasons,
            stop_conditions=stop_conditions,
        )
    selected_model = resolve_model(
        task,
        config,
        engine_name=selected_engine,
        model_override=model_override,
    )
    return [(task, selected_engine, engine_attempts, selected_model)], "single_task_complete"


def _print_pool_dry_run_plan(
    root: Path,
    *,
    planned_tasks: list[tuple[object, str, list[str], str | None]],
    blocked: list[object],
    config: LitehiveConfig,
    stop_conditions: TaskPoolStopConditions,
    predicted_stop_reason: str,
) -> None:
    print("dry_run: true")
    print(f"selection_policy: {config.pool_selection_policy}")
    print(f"planned_tasks: {len(planned_tasks)}")
    for index, (task, selected_engine, engine_attempts, selected_model) in enumerate(planned_tasks, start=1):
        checkpoints = ", ".join(task.human_checkpoints) if task.human_checkpoints else "-"
        model_label = selected_model or "-"
        print(
            f"would_run: {index}. {task.id} {task.title} "
            f"status={task.status} pipeline_status={task.pipeline_status} "
            f"engine={selected_engine} engine_attempts={', '.join(engine_attempts)} "
            f"model={model_label} human_checkpoints={checkpoints}"
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


def _budget_ledger_from_stop_conditions(
    stop_conditions: TaskPoolStopConditions,
) -> EngineBudgetLedger:
    return EngineBudgetLedger(
        pool_usage_cap=stop_conditions.pool_usage_cap,
        pool_cost_cap=stop_conditions.pool_cost_cap,
        engine_usage_caps=dict(stop_conditions.engine_usage_caps),
        engine_budget_caps=dict(stop_conditions.engine_budget_caps),
        engine_costs=dict(stop_conditions.engine_costs),
    )


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


def _parse_runner_hooks(
    raw_values: list[str] | None,
    *,
    option_name: str,
) -> dict[str, list[dict[str, object]]]:
    if not raw_values:
        return {}

    hooks: dict[str, list[dict[str, object]]] = {}
    for raw_value in raw_values:
        point, separator, remainder = raw_value.partition("=")
        if separator != "=":
            raise ValueError(f"{option_name} entries must use HOOK_POINT=blocking|nonblocking:COMMAND")
        blocking_label, separator, command = remainder.partition(":")
        if separator != ":":
            raise ValueError(f"{option_name} entries must use HOOK_POINT=blocking|nonblocking:COMMAND")
        blocking_key = blocking_label.strip().lower()
        if blocking_key not in {"blocking", "nonblocking", "non-blocking"}:
            raise ValueError(f"{option_name} blocking mode must be `blocking` or `nonblocking`")
        hooks.setdefault(point.strip(), []).append(
            {
                "command": command.strip(),
                "blocking": blocking_key == "blocking",
            }
        )
    return hooks


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
    configure.add_argument(
        "--pre-acceptance-command",
        default=None,
        help="Run this shell command after testing passes and before the task enters accepting",
    )
    configure.add_argument(
        "--hook",
        action="append",
        default=None,
        help=(
            "Add a runner hook as HOOK_POINT=blocking|nonblocking:COMMAND. "
            "Supported points: before_swe_implementation, after_swe_implementation, "
            "before_pm_acceptance, after_pm_acceptance."
        ),
    )
    configure.add_argument(
        "--subagent-resource-limits",
        dest="subagent_resource_limits_enabled",
        action="store_true",
        default=None,
        help="Enable container-level memory/CPU/process caps for subagent execution",
    )
    configure.add_argument(
        "--no-subagent-resource-limits",
        dest="subagent_resource_limits_enabled",
        action="store_false",
        help="Disable container-level memory/CPU/process caps for subagent execution",
    )
    configure.add_argument(
        "--subagent-memory-mb",
        type=int,
        default=None,
        help="Container memory cap in MiB for subagent execution",
    )
    configure.add_argument(
        "--subagent-cpu-count",
        type=float,
        default=None,
        help="Container CPU cap for subagent execution",
    )
    configure.add_argument(
        "--subagent-process-limit",
        type=int,
        default=None,
        help="Container process-count cap for subagent execution",
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

    repair = subparsers.add_parser(
        "repair",
        help="Repair stale active tasks, interrupted runs, and queue inconsistencies",
    )
    repair.add_argument(
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
        "--model",
        help="Preferred model override for supported engines on this task",
    )
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

    intake = subparsers.add_parser(
        "intake",
        help="Create a rough task from a freeform brain dump using an LLM",
    )
    intake.add_argument(
        "file",
        type=Path,
        nargs="?",
        help="File containing the brain dump; omit to read from stdin",
    )
    intake.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        default="opencode",
        help="Engine to use for analysis",
    )
    intake.add_argument(
        "--model",
        help="Model override for the selected engine",
    )
    intake.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )

    run = subparsers.add_parser("run", help="Run the next task once")

    run.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing .litehive/",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned selection for single-task or drain mode without invoking any agents",
    )
    run.add_argument(
        "--drain",
        action="store_true",
        help="Drain the task pool until it reaches an explicit stop condition",
    )
    run.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        help="Override the engine for this run only",
    )
    run.add_argument(
        "--model",
        help="Override the model for supported engines for this run only",
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

    requeue = subparsers.add_parser("requeue", help="Requeue a flagged or closed task")
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
        "resume", help="Resume an interrupted, flagged, or closed task from its current stage"
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
        "abandon", help="Cancel a flagged or closed task and remove it from the queue"
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
        "--follow-up-task",
        default=None,
        help="Optional existing task id linked as the follow-up for this close decision",
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
        "--model",
        help="Override task model, or use 'default' to clear the override",
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
        runner_hooks = _parse_runner_hooks(
            getattr(args, "hook", None),
            option_name="--hook",
        )
    except ValueError as exc:
        print(f"configure failed: {exc}")
        return 1

    try:
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
            pre_acceptance_command=getattr(args, "pre_acceptance_command", None),
            runner_hooks=runner_hooks,
            subagent_resource_limits=SubagentResourceLimitsConfig(
                enabled=getattr(args, "subagent_resource_limits_enabled", None),
                memory_mb=getattr(args, "subagent_memory_mb", None),
                cpu_count=getattr(args, "subagent_cpu_count", None),
                process_limit=getattr(args, "subagent_process_limit", None),
            ),
            task_engine_routing=task_engine_routing,
        )
    except ValueError as exc:
        print(f"configure failed: {exc}")
        return 1
    ensure_workspace(args.workspace, config)
    config_path(args.workspace).write_text(
        yaml.safe_dump(asdict(config), sort_keys=False),
        encoding="utf-8",
    )
    context_path(args.workspace).write_text(
        render_context_template(config.process_profile),
        encoding="utf-8",
    )
    print(f"Initialized litehive workspace in {args.workspace / '.litehive'}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.workspace)
    recover_stale_runner_state(args.workspace)
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
    print(f"execution_retry_policies: {_format_execution_retry_policies(config)}")
    print(f"pool_stop_on_failure: {config.pool_stop_on_failure}")
    print(f"pool_max_tasks: {config.pool_max_tasks}")
    print(f"pool_stop_on_execution_limit: {config.pool_stop_on_execution_limit}")
    print(f"pool_quota_threshold: {config.pool_quota_threshold}")
    print(f"pool_budget_threshold: {config.pool_budget_threshold}")
    print(f"pool_stop_on_dirty_git: {config.pool_stop_on_dirty_git}")
    print(f"pool_selection_policy: {config.pool_selection_policy}")
    print(f"pre_acceptance_command: {config.pre_acceptance_command}")
    print(f"runner_hooks: {format_runner_hooks(config)}")
    print(f"subagent_resource_limits: {format_subagent_resource_limits(config)}")
    print(f"external_engine_sandbox: {format_external_engine_sandbox(config)}")
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


def _task_model_label(task_model: str | None) -> str:
    return task_model or "default"


def _task_dependencies_label(task_id: str, dependencies: list[str]) -> str:
    if not dependencies:
        return "-"
    return (
        ", ".join(dependency_id for dependency_id in dependencies if dependency_id != task_id)
        or "-"
    )


def _task_interruption_label(task) -> str:
    if task.status != "interrupted" or task.runtime.current_stage.status != "interrupted":
        return ""
    interruption = task.runtime.interruption
    stage = (
        interruption.resume_stage
        if interruption is not None and interruption.resume_stage is not None
        else task.runtime.current_stage.step or task.pipeline_status
    )
    label = f" resumable_from={stage}"
    if interruption is not None:
        label += f" interruption={interruption.source}"
    if task.runtime.last_outcome.reason_code:
        label += f" reason_code={task.runtime.last_outcome.reason_code}"
    if task.runtime.last_subagent is not None:
        label += (
            " last_subagent="
            f"{task.runtime.last_subagent.id}:{task.runtime.last_subagent.role}/{task.runtime.last_subagent.engine}"
        )
    if interruption is not None and interruption.reason:
        label += f" reason={interruption.reason}"
    return label


def _cmd_queue(args: argparse.Namespace) -> int:
    config = load_config(args.workspace)
    recover_stale_runner_state(args.workspace)
    state = load_state(args.workspace)
    tasks = list_tasks(args.workspace)
    print(f"active_task_id: {state.active_task_id}")
    if state.active_task_id is not None:
        active_task = require_task(args.workspace, state.active_task_id)
        print(
            f"active: {active_task.id} [{active_task.status}/{active_task.pipeline_status}] "
            f"priority={active_task.priority} engine={_task_engine_label(active_task.engine, config.default_engine)} "
            f"model={_task_model_label(active_task.model)} "
            f"title={active_task.title} depends_on={_task_dependencies_label(active_task.id, active_task.depends_on)}"
            f"{_task_interruption_label(active_task)}"
        )
    print(f"queue_length: {len(state.queue)}")
    for index, task_id in enumerate(state.queue, start=1):
        task = require_task(args.workspace, task_id)
        print(
            f"{index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={_task_engine_label(task.engine, config.default_engine)} "
            f"model={_task_model_label(task.model)} "
            f"title={task.title} depends_on={_task_dependencies_label(task.id, task.depends_on)}"
            f"{_task_interruption_label(task)}"
        )
    resumable = [task for task in tasks if task.status == "interrupted"]
    print(f"resumable_tasks: {len(resumable)}")
    for index, task in enumerate(resumable, start=1):
        print(
            f"resume {index}. {task.id} [{task.status}/{task.pipeline_status}] "
            f"priority={task.priority} engine={_task_engine_label(task.engine, config.default_engine)} "
            f"model={_task_model_label(task.model)} "
            f"title={task.title} depends_on={_task_dependencies_label(task.id, task.depends_on)}"
            f"{_task_interruption_label(task)}"
        )
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        summary = repair_workspace_state(args.workspace)
    except WorkspaceConflictError as exc:
        print(f"repair failed: {exc}")
        return 1
    state = load_state(args.workspace)
    print(f"repaired: {'yes' if summary.mutated else 'no'}")
    print(f"stale_runner_recovered: {'yes' if summary.stale_runner_recovered else 'no'}")
    print(f"cleared_active_task_id: {summary.cleared_active_task_id or '-'}")
    print(
        "requeued_tasks: "
        + (" ".join(summary.requeued_task_ids) if summary.requeued_task_ids else "-")
    )
    print(
        "removed_queue_entries: "
        + (" ".join(summary.removed_queue_entries) if summary.removed_queue_entries else "-")
    )
    print(
        "deduped_queue_entries: "
        + (" ".join(summary.deduped_queue_entries) if summary.deduped_queue_entries else "-")
    )
    print(
        "restored_queue_entries: "
        + (" ".join(summary.restored_queue_entries) if summary.restored_queue_entries else "-")
    )
    print(
        "finalized_commit_tasks: "
        + (" ".join(summary.finalized_commit_task_ids) if summary.finalized_commit_task_ids else "-")
    )
    print(f"active_task_id: {state.active_task_id}")
    print(f"queue_length: {len(state.queue)}")
    return 0


def _launch_app(workspace: Path, default_mode: str) -> int:
    app = LitehiveApp(workspace=workspace, default_mode=default_mode)
    app.run()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
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
    args: argparse.Namespace,
    *,
    config: LitehiveConfig,
    stop_conditions: TaskPoolStopConditions,
    engine_override: str | None,
    model_override: str | None,
) -> int:
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
    completed: list[dict[str, object]] = []
    flagged: list[dict[str, object]] = []
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
        stage_outcomes = _task_stage_outcomes(args.workspace, execution.task.id, execution.task.slug)
        if stage_outcomes:
            print(f"stage_outcomes: {', '.join(stage_outcomes)}")
        if execution.commit_sha:
            print(f"commit: {execution.commit_sha}")
    for blocked in summary.blocked:
        print(f"blocked: {blocked.task_id} {blocked.title} blocked_by={', '.join(blocked.blocked_by)}")
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
    args: argparse.Namespace,
    *,
    config: LitehiveConfig,
    stop_conditions: TaskPoolStopConditions,
    engine_override: str | None,
    model_override: str | None,
) -> int:
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

    completed: list[dict[str, object]] = []
    flagged: list[dict[str, object]] = []
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
    print(f"next_commit_message: {checkpoint_message(summary.task)}")
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
    print(f"next_commit_message: {checkpoint_message(task)}")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def _cmd_move(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        state = move_queued_task(args.workspace, args.task_id, args.position)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"move failed: {exc}")
        return 1
    print(f"task_id: {args.task_id}")
    print(f"position: {state.queue.index(args.task_id) + 1}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = require_task(args.workspace, args.task_id)
        if task.status in {"interrupted", "flagged", "cancelled", "wont_do", "deferred", "duplicate"}:
            task = resume_task(args.workspace, args.task_id, front=True)
            print(f"task: {task.id} {task.title}")
            print("status: queued")
            print(f"pipeline_status: {task.pipeline_status}")
            missing_criteria_reason = missing_acceptance_criteria_reason(task)
            if missing_criteria_reason is not None:
                print(f"warning: {missing_criteria_reason}")
            print("position: 1")
            return 0
        move_queued_task(args.workspace, args.task_id, 1)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"promote failed: {exc}")
        return 1
    print(f"task_id: {args.task_id}")
    print("position: 1")
    return 0


def _cmd_prioritize(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        state = prioritize_queued_tasks(args.workspace, args.task_ids)
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"prioritize failed: {exc}")
        return 1
    print(f"moved_tasks: {' '.join(args.task_ids)}")
    print(f"moved_count: {len(args.task_ids)}")
    print(f"front_of_queue: {' '.join(state.queue[: len(args.task_ids)])}")
    print(f"queue_length: {len(state.queue)}")
    return 0


def _cmd_requeue_task(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    try:
        task = requeue_task(args.workspace, args.task_id, front=args.front)
    except (ValueError, WorkspaceConflictError) as exc:
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
    except (ValueError, WorkspaceConflictError) as exc:
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
        task = close_task(
            args.workspace,
            args.task_id,
            outcome=args.outcome,
            reason=args.reason,
            follow_up_task_id=args.follow_up_task,
        )
    except ValueError as exc:
        print(f"close failed: {exc}")
        return 1
    print(f"task: {task.id} {task.title}")
    print(f"status: {task.status}")
    print(f"outcome: {task.runtime.last_outcome.reason_code}")
    print(f"reason: {task.runtime.last_outcome.reason}")
    print(f"follow_up_task: {task.runtime.last_outcome.follow_up_task_id or '-'}")
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
            model=getattr(args, "model", None),
            retry_limit=getattr(args, "retry_limit", None),
            auto_commit=not args.no_auto_commit,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"add failed: {exc}")
        return 1
    print(
        f"Created task {task.id} in {args.workspace / '.litehive' / 'tasks' / (task.id + '-' + task.slug)}"
    )
    print(
        f"retry_limit: {task.retry_policy.max_retries if task.retry_policy.max_retries is not None else 'default'}"
    )
    print(f"mode: {task.mode}")
    print(f"engine: {_task_engine_label(task.engine, load_config(args.workspace).default_engine)}")
    print(f"model: {_task_model_label(task.model)}")
    print(
        "human_checkpoints: "
        + (", ".join(task.human_checkpoints) if task.human_checkpoints else "-")
    )
    print(f"task_type: {task.task_type or '-'}")
    print(f"depends_on: {_task_dependencies_label(task.id, task.depends_on)}")
    print(f"acceptance_criteria: {len(task.acceptance_criteria)}")
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
    if missing_criteria_reason is not None:
        print(f"warning: {missing_criteria_reason}")
    return 0


def _cmd_intake(args: argparse.Namespace) -> int:
    ensure_workspace(args.workspace)
    brain_dump = ""
    if args.file:
        try:
            brain_dump = args.file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Failed to read file: {exc}")
            return 1
    else:
        import sys

        try:
            if sys.stdin.isatty():
                print("Reading brain dump from stdin (Ctrl-D to end):")
            brain_dump = sys.stdin.read()
        except EOFError:
            pass

    if not brain_dump.strip():
        print("Empty brain dump; aborting.")
        return 1

    config = load_config(args.workspace)
    engine_name = args.engine or config.default_engine
    engine = get_engine(engine_name)
    model = args.model or (
        config.opencode_model if engine_name == "opencode" else None
    )

    prompt = intake_prompt(brain_dump)
    print(f"Analyzing brain dump with {engine_name}...")

    raw_title = _fallback_intake_title(brain_dump)
    raw_goal = ""

    try:
        execution = engine.run(prompt, cwd=args.workspace, model=model)
        if execution.exit_code == 0:
            transcript = engine.render_transcript(execution)
            from litehive.external_cli import _extract_line

            extracted_title = _extract_line(transcript, "TITLE")
            extracted_goal = _extract_line(transcript, "GOAL")

            if extracted_title:
                raw_title = extracted_title
            if extracted_goal:
                raw_goal = extracted_goal
        else:
            print(
                f"Warning: Analysis failed with exit code {execution.exit_code}. "
                "Creating task from raw intake."
            )
    except Exception as exc:
        print(f"Warning: Analysis failed ({exc}). Creating task from raw intake.")

    try:
        from litehive.tasks import task_dir, task_brief_file

        task_goal = raw_goal.strip() if raw_goal.strip() else _fallback_intake_goal(brain_dump)
        task_goal += f"\n\n(See intake.md for the original brain dump)"

        task = create_task(
            args.workspace,
            title=raw_title,
            goal=task_goal,
            mode="tasks",
            task_type="intake",
            engine=args.engine,
            model=args.model,
        )
        base = task_dir(args.workspace, task)
        (base / "intake.md").write_text(brain_dump, encoding="utf-8")

        brief_file = task_brief_file(args.workspace, task)
        _link_intake_brief_to_source(brief_file)

    except (ValueError, WorkspaceConflictError) as exc:
        print(f"Task creation failed: {exc}")
        return 1

    print(f"Created task {task.id}: {task.title}")
    if task.goal:
        print(f"Goal: {task.goal}")
    print(f"Original dump preserved at: {base / 'intake.md'}")
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
        and getattr(args, "model", None) is None
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
            model=(None if getattr(args, "model", None) == "default" else getattr(args, "model", None))
            if getattr(args, "model", None) is not None
            else ...,
            retry_limit=retry_limit,
            priority=args.priority if args.priority is not None else ...,
            goal=args.goal if args.goal is not None else ...,
            acceptance_criteria=acceptance_criteria,
            human_checkpoints=human_checkpoints,
            mode=args.mode if args.mode is not None else ...,
            auto_commit=args.auto_commit if args.auto_commit is not None else ...,
        )
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"update failed: {exc}")
        return 1
    config = load_config(args.workspace)
    print(f"task: {task.id} {task.title}")
    print(f"engine: {_task_engine_label(task.engine, config.default_engine)}")
    print(f"model: {_task_model_label(task.model)}")
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
    missing_criteria_reason = missing_acceptance_criteria_cli_warning(task)
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
    if args.command == "repair":
        return _cmd_repair(args)
    if args.command == "tasks":
        return _launch_app(args.workspace, default_mode="tasks")
    if args.command == "add":
        return _cmd_add(args)
    if args.command == "intake":
        return _cmd_intake(args)
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
