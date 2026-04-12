from litehive.cli.parsers.common import add_workspace_argument
from litehive.config import available_process_profiles, VALID_POOL_SELECTION_POLICIES


def register_configure_parser(subparsers):
    parser = subparsers.add_parser("configure", help="Initialize litehive workspace config")
    add_workspace_argument(
        parser,
        help_text="Repository root where .litehive/ should be created",
    )
    parser.add_argument(
        "--default-engine",
        default="codex",
        help="Default engine adapter name",
    )
    parser.add_argument(
        "--process-profile",
        choices=available_process_profiles(),
        default="generic",
        help="Prompt/process overlay preset for workspace initialization",
    )
    parser.add_argument(
        "--default-retry-limit",
        type=int,
        default=3,
        help="Default retry limit for tasks without a task-specific override",
    )
    parser.add_argument(
        "--litehive-source-path",
        default=None,
        help="Path to the Litehive source repo/workspace used for upstream issue filing and patch handoff",
    )
    parser.add_argument(
        "--opencode-model",
        default="zai-coding-plan/glm-5.1",
        help="Default model identifier when using the opencode adapter",
    )
    parser.add_argument(
        "--gemini-model",
        default=None,
        help="Default model identifier when using the gemini adapter",
    )
    parser.add_argument(
        "--copilot-model",
        default=None,
        help="Default model identifier when using the copilot adapter",
    )
    parser.add_argument(
        "--claude-model",
        default="claude-sonnet-4-20250514",
        help="Default model identifier when using the claude adapter",
    )
    parser.add_argument(
        "--claude-max-turns",
        type=int,
        default=30,
        help="Maximum conversation turns per claude invocation (guardrail against accidental quota burn)",
    )
    parser.add_argument(
        "--pool-usage-cap",
        type=int,
        default=None,
        help="Default pool behavior: stop before starting another engine invocation once this many invocations have run",
    )
    parser.add_argument(
        "--pool-cost-cap",
        type=int,
        default=None,
        help="Default pool behavior: stop before starting another engine invocation once this many cost units have been spent",
    )
    parser.add_argument(
        "--engine-usage-cap",
        action="append",
        default=None,
        help="Per-engine invocation cap as ENGINE=COUNT; repeat to set multiple engines",
    )
    parser.add_argument(
        "--engine-budget-cap",
        action="append",
        default=None,
        help="Per-engine budget cap in cost units as ENGINE=UNITS; repeat to set multiple engines",
    )
    parser.add_argument(
        "--engine-cost",
        action="append",
        default=None,
        help="Per-engine cost per invocation as ENGINE=UNITS; repeat to override defaults",
    )
    parser.add_argument(
        "--pool-stop-on-failure",
        action="store_true",
        help="Default pool behavior: stop after the first task that does not finish successfully",
    )
    parser.add_argument(
        "--pool-max-tasks",
        type=int,
        default=None,
        help="Default pool behavior: stop after completing this many tasks",
    )
    parser.add_argument(
        "--pool-stop-on-limit",
        action="store_true",
        help="Default pool behavior: stop after a quota, budget, rate, credit, or similar execution limit is hit",
    )
    parser.add_argument(
        "--pool-quota-threshold",
        type=int,
        default=None,
        help="Default pool behavior: stop after this many quota-like limit outcomes in a run",
    )
    parser.add_argument(
        "--pool-budget-threshold",
        type=int,
        default=None,
        help="Default pool behavior: stop after this many budget-like limit outcomes in a run",
    )
    parser.add_argument(
        "--pool-stop-on-dirty-git",
        action="store_true",
        help="Default pool behavior: stop when the git worktree is dirty before starting another task",
    )
    parser.add_argument(
        "--pool-selection-policy",
        choices=sorted(VALID_POOL_SELECTION_POLICIES),
        default="dependency_aware",
        help="Default pool task ordering policy",
    )
    parser.add_argument(
        "--hook",
        action="append",
        default=None,
        help=(
            "Add a runner hook as HOOK_POINT=reject|run:COMMAND. "
            "Supported points: before_grooming, after_grooming, before_implementing, "
            "after_implementing, before_testing, after_testing, before_accepting, "
            "after_accepting, after_commit. "
            "reject_on_failure only valid for after_implementing and after_testing."
        ),
    )
    parser.add_argument(
        "--subagent-resource-limits",
        dest="subagent_resource_limits_enabled",
        action="store_true",
        default=None,
        help="Enable container-level memory/CPU/process caps for subagent execution",
    )
    parser.add_argument(
        "--no-subagent-resource-limits",
        dest="subagent_resource_limits_enabled",
        action="store_false",
        help="Disable container-level memory/CPU/process caps for subagent execution",
    )
    parser.add_argument(
        "--subagent-memory-mb",
        type=int,
        default=None,
        help="Container memory cap in MiB for subagent execution",
    )
    parser.add_argument(
        "--subagent-cpu-count",
        type=float,
        default=None,
        help="Container CPU cap for subagent execution",
    )
    parser.add_argument(
        "--subagent-process-limit",
        type=int,
        default=None,
        help="Container process-count cap for subagent execution",
    )
