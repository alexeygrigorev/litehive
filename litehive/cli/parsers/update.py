from pathlib import Path

from litehive.cli._parse import TASK_TYPE_CHOICES
from litehive.cli.parsers._common import add_workspace_argument
from litehive.tasks.constants import (
    VALID_HUMAN_CHECKPOINTS,
    VALID_PM_COMPLEXITIES,
    VALID_PLANNED_EFFORTS,
    VALID_TASK_PRIORITIES,
)


def register_update_parser(subparsers):
    parser = subparsers.add_parser("update", help="Update task metadata")
    parser.add_argument("task_id", help="Task id to update")
    parser.add_argument(
        "--model",
        help="Override task model, or use 'default' to clear the override",
    )
    parser.add_argument(
        "--retry-limit",
        type=str,
        help="Set task retry limit, or use 'default' to clear the override",
    )
    parser.add_argument(
        "--priority",
        choices=sorted(VALID_TASK_PRIORITIES),
        help="Set task priority",
    )
    parser.add_argument(
        "--pm-complexity",
        choices=[*sorted(VALID_PM_COMPLEXITIES), "none"],
        help="Set PM complexity, or use 'none' to clear it",
    )
    parser.add_argument(
        "--planned-effort",
        choices=[*sorted(VALID_PLANNED_EFFORTS), "none"],
        help="Set planned effort, or use 'none' to clear it",
    )
    parser.add_argument("--goal", help="Replace the task goal text")
    parser.add_argument(
        "--depends-on",
        action="append",
        help="Replace task dependencies; repeat the flag or use a comma-separated list, or use 'none' to clear",
    )
    parser.add_argument(
        "--acceptance-criteria",
        action="append",
        default=None,
        help="Replace acceptance criteria; repeat the flag or use 'none' to clear",
    )
    parser.add_argument(
        "--constraint",
        action="append",
        default=None,
        help="Replace constraints; repeat the flag or use 'none' to clear",
    )
    parser.add_argument(
        "--plan-step",
        action="append",
        default=None,
        help="Replace the task plan; repeat the flag or use 'none' to clear",
    )
    parser.add_argument(
        "--human-checkpoint",
        action="append",
        default=None,
        choices=[*sorted(VALID_HUMAN_CHECKPOINTS), "none"],
        help="Replace human checkpoints; repeat the flag or use 'none' to clear",
    )
    parser.add_argument(
        "--task-type",
        choices=[*TASK_TYPE_CHOICES, "default"],
        help="Override task routing class, or use 'default' to clear it",
    )
    parser.add_argument(
        "--mode",
        choices=["tasks", "implementation"],
        help="Set task mode",
    )
    parser.add_argument(
        "--auto-commit",
        dest="auto_commit",
        action="store_true",
        default=None,
        help="Enable auto-commit for this task",
    )
    parser.add_argument(
        "--no-auto-commit",
        dest="auto_commit",
        action="store_false",
        help="Disable auto-commit for this task",
    )
    parser.add_argument(
        "--from-file",
        type=Path,
        help="Load rich task updates from a YAML file mapped onto durable task fields",
    )
    parser.add_argument(
        "--edit",
        action="store_true",
        help="Open the current task shaping fields in $VISUAL or $EDITOR and persist the edited YAML",
    )
    add_workspace_argument(parser)
