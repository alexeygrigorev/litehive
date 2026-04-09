from litehive.cli.parsers._common import add_workspace_argument


def register_import_issue_parser(subparsers):
    parser = subparsers.add_parser(
        "import-issue",
        help="Import a single GitHub issue as a litehive task",
    )
    parser.add_argument(
        "issue_ref",
        help="GitHub issue URL (https://github.com/owner/repo/issues/N) or issue number",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repo as owner/repo; auto-detected from git remote if omitted",
    )
    add_workspace_argument(parser)


def register_import_issues_parser(subparsers):
    parser = subparsers.add_parser(
        "import-issues",
        help="Import all open GitHub issues that don't already have litehive tasks",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repo as owner/repo; auto-detected from git remote if omitted",
    )
    add_workspace_argument(parser)
