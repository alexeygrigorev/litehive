from litehive.cli.parsers._common import add_workspace_argument


def register_resume_parser(subparsers):
    parser = subparsers.add_parser(
        "resume",
        help="Resume an interrupted, parked, flagged, or closed task from its current stage",
    )
    parser.add_argument("task_id", help="Task id to resume")
    parser.add_argument(
        "--front",
        action="store_true",
        help="Insert the task at the front of the queue",
    )
    add_workspace_argument(parser)
