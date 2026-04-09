from litehive.cli.parsers._common import add_workspace_argument


def register_cleanup_parser(subparsers):
    parser = subparsers.add_parser(
        "cleanup", help="Delete archived tasks older than a given duration"
    )
    parser.add_argument(
        "--older-than",
        required=True,
        dest="older_than",
        help="Duration threshold (e.g. 30d, 24h, 60m)",
    )
    add_workspace_argument(parser)
