from litehive.cli.parsers.common import add_workspace_argument


def register_show_parser(subparsers):
    parser = subparsers.add_parser(
        "show", help="Print full details for a single task"
    )
    parser.add_argument("task_id", help="Task ID (e.g. T-0001)")
    add_workspace_argument(parser)
