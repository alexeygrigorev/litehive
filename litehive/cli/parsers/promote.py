from litehive.cli.parsers.common import add_workspace_argument


def register_promote_parser(subparsers):
    parser = subparsers.add_parser("promote", help="Move a queued task to the front of the queue")
    parser.add_argument("task_id", help="Queued task id to promote")
    add_workspace_argument(parser)
