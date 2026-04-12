from litehive.cli.parsers.common import add_workspace_argument


def register_backup_parser(subparsers):
    parser = subparsers.add_parser("backup", help="Create, list, and restore workspace database backups")
    parser.set_defaults(command_parser=parser)
    add_workspace_argument(parser)
    backup_subparsers = parser.add_subparsers(dest="backup_command")

    create_parser = backup_subparsers.add_parser(
        "create", help="Create a compressed backup of the workspace runtime database"
    )
    add_workspace_argument(create_parser)

    list_parser = backup_subparsers.add_parser(
        "list", help="List available workspace runtime database backups"
    )
    add_workspace_argument(list_parser)

    restore_parser = backup_subparsers.add_parser(
        "restore", help="Restore a workspace runtime database backup"
    )
    restore_parser.add_argument("timestamp", help="Backup timestamp shown by `litehive backup list`")
    restore_parser.add_argument("--yes", action="store_true", help="Skip the overwrite confirmation prompt")
    add_workspace_argument(restore_parser)
