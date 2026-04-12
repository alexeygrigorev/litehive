from litehive.cli.parsers.common import add_workspace_argument


def register_dirty_worktree_gate_parser(subparsers):
    parser = subparsers.add_parser(
        "dirty-worktree-gate",
        help="Report whether dirty git state should block the workspace and explain ownership",
    )
    add_workspace_argument(parser)
