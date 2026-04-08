from dataclasses import asdict

import yaml

from litehive.config import (
    LitehiveConfig,
    SubagentResourceLimitsConfig,
    config_path,
    context_path,
    ensure_workspace,
    load_config,
    render_context_template,
)
from litehive.cli._parse import (
    _parse_engine_int_map,
    _parse_runner_hooks,
)


def _cmd_configure(args):
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
            litehive_source_path=getattr(args, "litehive_source_path", None),
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
            runner_hooks=runner_hooks,
            subagent_resource_limits=SubagentResourceLimitsConfig(
                enabled=getattr(args, "subagent_resource_limits_enabled", None),
                memory_mb=getattr(args, "subagent_memory_mb", None),
                cpu_count=getattr(args, "subagent_cpu_count", None),
                process_limit=getattr(args, "subagent_process_limit", None),
            ),
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
