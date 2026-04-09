from litehive.agents import ENGINE_CHOICES
from litehive.cli._parse import TASK_TYPE_CHOICES
from litehive.cli.parsers._common import add_workspace_argument
from litehive.tasks.constants import (
    VALID_HUMAN_CHECKPOINTS,
    VALID_PM_COMPLEXITIES,
    VALID_PLANNED_EFFORTS,
    VALID_TASK_PRIORITIES,
)


def register_add_parser(subparsers):
    parser = subparsers.add_parser("add", help="Create a queued task")
    parser.add_argument("title", help="Task title")
    parser.add_argument("--goal", default="", help="Task goal text")
    parser.add_argument(
        "--pm-complexity",
        choices=sorted(VALID_PM_COMPLEXITIES),
        help="Initial PM complexity estimate",
    )
    parser.add_argument(
        "--planned-effort",
        choices=sorted(VALID_PLANNED_EFFORTS),
        help="Initial PM planned effort size",
    )
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
        "--human-checkpoint",
        action="append",
        default=None,
        choices=sorted(VALID_HUMAN_CHECKPOINTS),
        help="Pause the task before this stage boundary; repeat for multiple checkpoints",
    )
    parser.add_argument(
        "--task-type",
        choices=TASK_TYPE_CHOICES,
        help="Explicit routing class for this task",
    )
    parser.add_argument(
        "--single",
        action="store_true",
        default=False,
        help="Use single-agent pipeline mode: one agent completes the task with no grooming, QA, or review stages",
    )
    parser.add_argument(
        "--mode",
        choices=["implementation", "tasks"],
        help="Task creation mode; defaults to `tasks` when `--task-type` is set, otherwise `implementation`",
    )
    parser.add_argument(
        "--priority",
        choices=sorted(VALID_TASK_PRIORITIES),
        help="Task priority; defaults to medium when omitted",
    )
    parser.add_argument("--engine", choices=ENGINE_CHOICES, help="Preferred engine for the task")
    parser.add_argument(
        "--model",
        help="Preferred model override for supported engines on this task",
    )
    parser.add_argument(
        "--retry-limit",
        type=int,
        help="Per-task retry limit override; omit to use the workspace default",
    )
    parser.add_argument(
        "--no-auto-commit",
        action="store_true",
        help="Disable auto-commit for this task",
    )
    add_workspace_argument(parser)
