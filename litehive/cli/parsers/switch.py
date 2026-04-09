from litehive.agents import ENGINE_CHOICES
from litehive.cli.parsers._common import add_workspace_argument


def register_switch_parser(subparsers):
    parser = subparsers.add_parser(
        "switch",
        help="Stop or resume a task, switch its task-level engine override, and queue it for the next iteration",
    )
    parser.add_argument("task_id", help="Task id to switch")
    parser.add_argument(
        "engine", choices=sorted(ENGINE_CHOICES), help="Task-level engine override to persist"
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Why the engine switch happened; recorded in the task thread comment",
    )
    add_workspace_argument(parser)
