from litehive.cli.parsers._common import add_workspace_argument
from litehive.cli.parsers.cleanup import register_cleanup_parser


def register_archive_parser(subparsers):
    parser = subparsers.add_parser(
        "archive", help="Move done tasks to the archive directory"
    )
    parser.set_defaults(command_parser=parser)
    parser.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task ID to archive",
    )
    parser.add_argument(
        "--all-done",
        action="store_true",
        help="Archive all done tasks and skip missing or broken task references",
    )
    add_workspace_argument(parser)
    archive_subparsers = parser.add_subparsers(dest="archive_command")
    register_cleanup_parser(archive_subparsers)
