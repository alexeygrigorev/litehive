from litehive.cli.parse import TASK_TYPE_CHOICES
from litehive.cli.parsers.common import add_workspace_argument
from litehive.tasks.constants import VALID_TASK_PRIORITIES


def register_add_parser(subparsers):
    parser = subparsers.add_parser("add", help="Create a queued task")
    parser.add_argument("title", help="Task title")
    parser.add_argument("--goal", default="", help="Task goal text")
    parser.add_argument(
        "--acceptance-criteria",
        action="append",
        default=None,
        help="Add one acceptance criterion; repeat the flag for structured criteria",
    )
    parser.add_argument(
        "--depends-on",
        action="append",
        help="Add prerequisite task ids; repeat the flag or use a comma-separated list",
    )
    parser.add_argument(
        "--task-type",
        choices=TASK_TYPE_CHOICES,
        help="Explicit routing class for this task",
    )
    parser.add_argument(
        "--mode",
        choices=["full", "single"],
        help="Task pipeline mode; defaults to `full`",
    )
    parser.add_argument(
        "--priority",
        choices=sorted(VALID_TASK_PRIORITIES),
        help="Task priority; defaults to medium when omitted",
    )
    add_workspace_argument(parser)
