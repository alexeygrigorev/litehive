from litehive.cli.parsers._common import add_workspace_argument


def register_status_parser(subparsers):
    parser = subparsers.add_parser("status", help="Show workspace status")
    add_workspace_argument(parser)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include the full per-task status dump.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Deprecated compatibility alias; fast status is now the default",
    )
