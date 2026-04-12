from litehive.agents import ENGINE_CHOICES
from litehive.cli.parsers.common import add_workspace_argument


def register_run_parser(subparsers):
    parser = subparsers.add_parser("run", help="Run the next task once")
    add_workspace_argument(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned selection for single-task or drain mode without invoking any agents",
    )
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Drain the task pool until it reaches an explicit stop condition",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run multiple independent tasks in parallel using separate worktrees (task-level parallelism)",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        help="Override the engine for this run only",
    )
    parser.add_argument(
        "--model",
        help="Override the model for supported engines for this run only",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        default=None,
        help="Stop the pool after the first task that does not finish successfully",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        help="Stop the pool after completing this many tasks",
    )
    parser.add_argument(
        "--stop-on-limit",
        action="store_true",
        default=None,
        help="Stop the pool after a quota, budget, rate, credit, or similar execution limit is hit",
    )
    parser.add_argument(
        "--quota-threshold",
        type=int,
        help="Stop the pool after this many quota-like limit outcomes in the current run",
    )
    parser.add_argument(
        "--budget-threshold",
        type=int,
        help="Stop the pool after this many budget-like limit outcomes in the current run",
    )
    parser.add_argument(
        "--pool-usage-cap",
        type=int,
        help="Stop before starting another engine invocation once this many invocations have run in the current pool",
    )
    parser.add_argument(
        "--pool-cost-cap",
        type=int,
        help="Stop before starting another engine invocation once this many cost units have been spent in the current pool",
    )
    parser.add_argument(
        "--engine-usage-cap",
        action="append",
        default=None,
        help="Per-engine invocation cap for this run as ENGINE=COUNT; repeat to set multiple engines",
    )
    parser.add_argument(
        "--engine-budget-cap",
        action="append",
        default=None,
        help="Per-engine budget cap for this run in cost units as ENGINE=UNITS; repeat to set multiple engines",
    )
    parser.add_argument(
        "--engine-cost",
        action="append",
        default=None,
        help="Per-engine cost for this run as ENGINE=UNITS; repeat to override defaults",
    )
    parser.add_argument(
        "--stop-on-dirty-git",
        action="store_true",
        default=None,
        help="Stop the pool when the git worktree is dirty before starting another task",
    )
