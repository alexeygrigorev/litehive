from litehive.cli.parsers.common import add_workspace_argument


def register_tasks_parser(subparsers):
    parser = subparsers.add_parser("tasks", help="Open the task view")
    add_workspace_argument(parser)
