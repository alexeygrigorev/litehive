from litehive.cli.parsers.common import add_workspace_argument


def register_repair_parser(subparsers):
    parser = subparsers.add_parser(
        "repair",
        help="Repair stale active tasks, interrupted runs, and queue inconsistencies",
    )
    add_workspace_argument(parser)
