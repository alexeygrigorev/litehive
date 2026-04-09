from litehive.cli.parsers._common import add_workspace_argument


def register_stop_parser(subparsers):
    parser = subparsers.add_parser("stop", help="Stop the current active task cleanly")
    add_workspace_argument(parser)
