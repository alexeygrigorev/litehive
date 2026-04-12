from litehive.cli.parsers.common import add_workspace_argument


def register_doctor_parser(subparsers):
    parser = subparsers.add_parser(
        "doctor",
        help="Run workspace integrity checks and optional safe fixes",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply deterministic non-destructive fixes where available",
    )
    add_workspace_argument(parser)
