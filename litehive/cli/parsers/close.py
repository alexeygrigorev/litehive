from litehive.cli.parsers.common import add_workspace_argument


def register_close_parser(subparsers):
    parser = subparsers.add_parser(
        "close",
        help="Close a task with an explicit non-implementation outcome (wont_do, deferred, duplicate)",
    )
    parser.add_argument("task_id", help="Task id to close")
    parser.add_argument(
        "--outcome",
        required=True,
        choices=["wont_do", "deferred", "duplicate"],
        help="Reason the task is being closed without implementation",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="Optional free-text rationale recorded in the task journal",
    )
    parser.add_argument(
        "--follow-up-task",
        default=None,
        help="Optional existing task id linked as the follow-up for this close decision",
    )
    add_workspace_argument(parser)
