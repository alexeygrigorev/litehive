from litehive.cli.parsers._common import add_workspace_argument


def register_start_parser(subparsers):
    parser = subparsers.add_parser("start", help="Start the background Litehive runner")
    add_workspace_argument(parser)


def register_stop_daemon_parser(subparsers):
    parser = subparsers.add_parser("stop", help="Stop the background Litehive runner")
    add_workspace_argument(parser)


def register_restart_parser(subparsers):
    parser = subparsers.add_parser("restart", help="Restart the background Litehive runner")
    add_workspace_argument(parser)
