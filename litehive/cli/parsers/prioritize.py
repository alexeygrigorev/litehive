from litehive.cli.parsers.common import add_workspace_argument


def register_prioritize_parser(subparsers):
    parser = subparsers.add_parser(
        "prioritize",
        help="Move multiple queued tasks to the front in the requested order",
    )
    parser.add_argument(
        "task_ids",
        nargs="+",
        help="Queued task ids to move to the front, in the requested order",
    )
    add_workspace_argument(parser)
