from litehive.cli.parsers._common import add_workspace_argument
from litehive.cli.parsers.abandon import register_abandon_parser
from litehive.cli.parsers.add import register_add_parser
from litehive.cli.parsers.close import register_close_parser
from litehive.cli.parsers.debug import register_debug_parser
from litehive.cli.parsers.list import register_list_parser
from litehive.cli.parsers.logs import register_logs_parser
from litehive.cli.parsers.show import register_show_parser
from litehive.cli.parsers.update import register_update_parser


def register_task_parser(subparsers):
    parser = subparsers.add_parser("task", help="Manage Litehive tasks")
    add_workspace_argument(parser)
    task_subparsers = parser.add_subparsers(dest="task_command")
    register_add_parser(task_subparsers)
    register_list_parser(task_subparsers)
    register_show_parser(task_subparsers)
    register_update_parser(task_subparsers)
    register_close_parser(task_subparsers)
    register_abandon_parser(task_subparsers)
    register_debug_parser(task_subparsers)
    register_logs_parser(task_subparsers)
