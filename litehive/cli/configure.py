from dataclasses import asdict

import yaml

from litehive.config import (
    LitehiveConfig,
    config_path,
    context_path,
    ensure_workspace,
    render_context_template,
)
from litehive.cli.parse import parse_runner_hooks


def cmd_configure(args):
    try:
        runner_hooks = parse_runner_hooks(
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
            claude_model=getattr(args, "claude_model", "claude-sonnet-4-20250514"),
            claude_max_turns=getattr(args, "claude_max_turns", 30),
            pool_stop_on_failure=getattr(args, "pool_stop_on_failure", False),
            pool_max_tasks=getattr(args, "pool_max_tasks", None),
            pool_stop_on_dirty_git=getattr(args, "pool_stop_on_dirty_git", False),
            pool_selection_policy=getattr(args, "pool_selection_policy", "dependency_aware"),
            runner_hooks=runner_hooks,
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
