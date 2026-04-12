from pathlib import Path

from litehive.cli.parsers.common import add_workspace_argument
from litehive.tasks.constants import VALID_TASK_PRIORITIES


def register_update_parser(subparsers):
    parser = subparsers.add_parser("update", help="Update task metadata")
    parser.add_argument("task_id", help="Task id to update")
    parser.add_argument("--title", help="Replace the task title")
    parser.add_argument(
        "--priority",
        choices=sorted(VALID_TASK_PRIORITIES),
        help="Set task priority",
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
