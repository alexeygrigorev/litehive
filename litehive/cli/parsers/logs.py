from litehive.cli.parsers.common import add_workspace_argument


def register_logs_parser(subparsers):
    parser = subparsers.add_parser(
        "logs",
        help="Show daemon, task journal, and subagent logs",
    )
    parser.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Optional task ID (e.g. T-0001)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="List the latest daemon run-all sessions",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Show subagent transcript/stdout instead of the task journal",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="List all subagent runs for the task (requires --agent)",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Follow the currently running subagent stdout in real time",
    )
    add_workspace_argument(parser)
