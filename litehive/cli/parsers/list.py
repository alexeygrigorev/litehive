from litehive.cli.parsers._common import add_workspace_argument


def register_list_parser(subparsers):
    parser = subparsers.add_parser(
        "list", help="Compact task listing with optional filters"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Include done tasks (excluded by default)",
    )
    parser.add_argument(
        "--status",
        dest="filter_status",
        help="Filter by task status (queued, in_progress, done, ...)",
    )
    parser.add_argument(
        "--pipeline-status",
        dest="filter_pipeline_status",
        help="Filter by pipeline stage (backlog, implementing, ...)",
    )
    parser.add_argument(
        "--engine",
        dest="filter_engine",
        help="Filter by engine name",
    )
    add_workspace_argument(parser)
