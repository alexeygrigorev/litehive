from litehive.cli.parsers.common import add_workspace_argument


def register_health_parser(subparsers):
    parser = subparsers.add_parser("health", help="Show workspace health diagnostics")
    add_workspace_argument(parser)
