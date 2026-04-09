from litehive.cli.parsers._common import add_workspace_argument


def register_abandon_parser(subparsers):
    parser = subparsers.add_parser(
        "abandon", help="Cancel a flagged or closed task and remove it from the queue"
    )
    parser.add_argument("task_id", help="Task id to abandon")
    add_workspace_argument(parser)
