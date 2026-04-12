from litehive.cli.parsers.common import add_workspace_argument


def register_requeue_parser(subparsers):
    parser = subparsers.add_parser("requeue", help="Requeue a flagged or closed task")
    parser.add_argument("task_id", help="Task id to requeue")
    parser.add_argument(
        "--front",
        action="store_true",
        help="Insert the task at the front of the queue",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force requeue even if the task has been flagged 3+ times",
    )
    add_workspace_argument(parser)
