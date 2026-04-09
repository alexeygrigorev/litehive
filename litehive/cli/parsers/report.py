from litehive.cli.parsers._common import add_workspace_argument


def register_report_parser(subparsers):
    parser = subparsers.add_parser("report", help="Submit a stage verdict for the active task")
    parser.add_argument("--verdict", required=True, choices=["pass", "fail", "reject", "comment"])
    parser.add_argument(
        "--message",
        required=True,
        help="Detailed explanation of what was done, what failed, or what needs fixing",
    )
    parser.add_argument(
        "--role", default="swe", help="Role submitting the report (swe, qa, reviewer, planner)"
    )
    parser.add_argument(
        "--step",
        help="Stage (grooming, implementing, testing, accepting). Defaults to the task's current pipeline_status.",
    )
    parser.add_argument("--task-id", help="Task ID. Defaults to the active task.")
    add_workspace_argument(parser)
