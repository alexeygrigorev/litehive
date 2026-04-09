from litehive.cli.parsers._common import add_workspace_argument


def register_debug_parser(subparsers):
    parser = subparsers.add_parser(
        "debug", help="Inspect subagent artifacts for a task"
    )
    parser.add_argument("task_id", help="Task ID (e.g. T-0001)")
    parser.add_argument(
        "--all",
        action="store_true",
        help="List all subagents with their status and exit code",
    )
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="Show whether the task worktree exists plus uncommitted and committed changes",
    )
    add_workspace_argument(parser)
