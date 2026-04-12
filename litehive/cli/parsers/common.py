from pathlib import Path


def add_workspace_argument(parser, help_text="Repository root containing .litehive/"):
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help=help_text,
    )
