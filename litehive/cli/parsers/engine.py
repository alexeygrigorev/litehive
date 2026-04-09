from litehive.agents import ENGINE_CHOICES
from litehive.cli.parsers._common import add_workspace_argument


def register_engine_parser(subparsers):
    parser = subparsers.add_parser(
        "engine",
        help="Manage the workspace default engine",
    )
    parser.add_argument(
        "engine_action",
        choices=[*ENGINE_CHOICES, "set", "freeze", "unfreeze", "status"],
        help="Engine name (shorthand for 'set') or subcommand: set, freeze, unfreeze, status",
    )
    parser.add_argument(
        "engine_name",
        nargs="?",
        default=None,
        help="Engine name (required for set/freeze/unfreeze subcommands)",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Freeze until this date/datetime (local timezone, e.g. 2026-04-08 or '2026-04-08 09:47')",
    )
    add_workspace_argument(parser)
