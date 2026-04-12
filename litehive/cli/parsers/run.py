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
        "--stop-on-dirty-git",
        action="store_true",
        default=None,
        help="Stop the pool when the git worktree is dirty before starting another task",
    )
