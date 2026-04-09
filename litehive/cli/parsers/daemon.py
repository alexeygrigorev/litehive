import argparse

from litehive.cli.parsers._common import add_workspace_argument


def register_daemon_parser(subparsers):
    parser = subparsers.add_parser("daemon", help="Manage the Litehive pool daemon")
    daemon_subparsers = parser.add_subparsers(dest="daemon_command")

    run = daemon_subparsers.add_parser("run", help="Start the workspace daemon")
    add_workspace_argument(run)
    run.add_argument(
        "--foreground",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    status = daemon_subparsers.add_parser("status", help="Show daemon state for a workspace")
    add_workspace_argument(status)

    stop = daemon_subparsers.add_parser("stop", help="Stop the workspace daemon")
    add_workspace_argument(stop)

    restart = daemon_subparsers.add_parser("restart", help="Restart the workspace daemon")
    add_workspace_argument(restart)

    daemon_subparsers.add_parser("instances", help="List all live Litehive daemons")

    worker = daemon_subparsers.add_parser("worker", help=argparse.SUPPRESS)
    add_workspace_argument(worker, help_text=argparse.SUPPRESS)
