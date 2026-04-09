from litehive.cli.parsers._common import add_workspace_argument


def register_move_parser(subparsers):
    parser = subparsers.add_parser("move", help="Move a queued task to a 1-based position")
    parser.add_argument("task_id", help="Queued task id to move")
    parser.add_argument("position", type=int, help="Target queue position (1-based)")
    add_workspace_argument(parser)
