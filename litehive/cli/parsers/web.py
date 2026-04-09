from litehive.cli.parsers._common import add_workspace_argument


def register_web_parser(subparsers):
    parser = subparsers.add_parser("web", help="Serve the local queue and session monitor")
    add_workspace_argument(parser)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1 for local-only access)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="TCP port to bind",
    )
