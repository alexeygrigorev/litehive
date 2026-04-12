from litehive.cli.parsers.common import add_workspace_argument


def register_recover_parser(subparsers):
    parser = subparsers.add_parser(
        "recover", help="Requeue a completed task without reverting code"
    )
    parser.add_argument("task_id", help="Task id to recover")
    add_workspace_argument(parser)
