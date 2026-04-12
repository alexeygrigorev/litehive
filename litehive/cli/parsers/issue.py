from pathlib import Path

from litehive.cli.parsers.common import add_workspace_argument

UPSTREAM_CONTRIBUTION_CHOICES = [
    "runtime_bug",
    "missing_feature",
    "config_improvement",
    "prompt_improvement",
    "engine_adapter_fix",
]


def register_issue_parser(subparsers):
    parser = subparsers.add_parser(
        "issue",
        help="File an upstream Litehive issue/task from the current project",
    )
    parser.add_argument(
        "--upstream",
        required=True,
        help="Upstream Litehive issue title or short summary",
    )
    parser.add_argument(
        "--type",
        choices=UPSTREAM_CONTRIBUTION_CHOICES,
        default="runtime_bug",
        help="Contribution class for the upstream task",
    )
    parser.add_argument(
        "--details",
        default="",
        help="Long-form details, reproduction notes, or requested change",
    )
    parser.add_argument(
        "--acceptance-criteria",
        action="append",
        default=None,
        help="Add acceptance criteria for the upstream task; repeat for multiple",
    )
    parser.add_argument(
        "--source-task",
        default=None,
        help="Originating task id in the current project; defaults to the active task if any",
    )
    parser.add_argument(
        "--source-stage",
        default=None,
        help="Originating pipeline stage in the current project",
    )
    parser.add_argument(
        "--source-role",
        default="recovery",
        help="Role filing the upstream task",
    )
    parser.add_argument(
        "--source-project",
        default=None,
        help="Override the source project name shown in the upstream task",
    )
    parser.add_argument(
        "--litehive-workspace",
        type=Path,
        default=None,
        help="Override the target Litehive repo/workspace instead of using litehive_source_path",
    )
    parser.add_argument(
        "--patch-branch",
        default=None,
        help="Branch name in the Litehive repo for a proposed fix handoff",
    )
    parser.add_argument(
        "--patch-base",
        default="HEAD",
        help="Base ref used when preparing --patch-branch (default: HEAD)",
    )
    parser.add_argument(
        "--prepare-patch-branch",
        action="store_true",
        help="Create the patch branch in the Litehive repo before filing the task",
    )
    add_workspace_argument(parser)
