from litehive.cli.parsers._common import add_workspace_argument


def register_queue_parser(subparsers):
    parser = subparsers.add_parser("queue", help="Show the active task and queued order")
    add_workspace_argument(parser)
