from litehive.cli.parsers._common import add_workspace_argument


def register_worktree_parser(subparsers):
    parser = subparsers.add_parser(
        "worktree",
        help="Inspect and clean Litehive-managed task worktrees",
    )
    worktree_subparsers = parser.add_subparsers(dest="worktree_command")

    ls = worktree_subparsers.add_parser(
        "ls",
        help="List Litehive-managed task worktrees with task status and change count",
    )
    add_workspace_argument(ls)

    clean = worktree_subparsers.add_parser(
        "clean",
        help="Remove Litehive-managed worktrees for closed tasks",
    )
    clean.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which worktrees would be removed without removing them",
    )
    add_workspace_argument(clean)
