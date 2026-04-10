from litehive.cli.parsers._common import add_workspace_argument
from litehive.cli.parsers.move import register_move_parser
from litehive.cli.parsers.promote import register_promote_parser
from litehive.cli.parsers.requeue import register_requeue_parser
from litehive.cli.parsers.resume import register_resume_parser
from litehive.cli.parsers.stop import register_stop_parser


def register_queue_parser(subparsers):
    parser = subparsers.add_parser("queue", help="Show the active task and queued order")
    add_workspace_argument(parser)
    queue_subparsers = parser.add_subparsers(dest="queue_command")
    register_move_parser(queue_subparsers)
    register_promote_parser(queue_subparsers)
    register_requeue_parser(queue_subparsers)
    register_resume_parser(queue_subparsers)
    register_stop_parser(queue_subparsers)
