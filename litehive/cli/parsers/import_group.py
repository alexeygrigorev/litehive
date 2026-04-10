from pathlib import Path

from litehive.agents import ENGINE_CHOICES
from litehive.cli.parsers._common import add_workspace_argument
from litehive.cli.parsers.issue import register_issue_parser


def register_import_parser(subparsers):
    parser = subparsers.add_parser("import", help="Import or file tasks from external inputs")
    add_workspace_argument(parser)
    import_subparsers = parser.add_subparsers(dest="import_command")

    github = import_subparsers.add_parser(
        "github",
        help="Import GitHub issues as Litehive tasks",
    )
    github.add_argument(
        "issue_ref",
        nargs="?",
        default=None,
        help="GitHub issue URL (https://github.com/owner/repo/issues/N) or issue number",
    )
    github.add_argument(
        "--repo",
        default=None,
        help="GitHub repo as owner/repo; auto-detected from git remote if omitted",
    )
    github.add_argument(
        "--all",
        action="store_true",
        help="Import all open GitHub issues that do not already have Litehive tasks",
    )
    add_workspace_argument(github)

    spec = import_subparsers.add_parser(
        "spec",
        help="Create a rough task from a freeform spec using an LLM",
    )
    spec.add_argument(
        "file",
        type=Path,
        nargs="?",
        help="File containing the brain dump; omit to read from stdin",
    )
    spec.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        default="opencode",
        help="Engine to use for analysis",
    )
    spec.add_argument(
        "--model",
        help="Model override for the selected engine",
    )
    add_workspace_argument(spec)

    register_issue_parser(import_subparsers)
