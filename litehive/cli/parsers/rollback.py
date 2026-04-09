from litehive.cli.parsers._common import add_workspace_argument


def register_rollback_parser(subparsers):
    parser = subparsers.add_parser(
        "rollback", help="Revert a task checkpoint commit and requeue the task"
    )
    parser.add_argument("task_id", help="Task id to roll back")
    add_workspace_argument(parser)
