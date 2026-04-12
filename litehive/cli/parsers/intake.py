from pathlib import Path

from litehive.agents import ENGINE_CHOICES
from litehive.cli.parsers.common import add_workspace_argument


def register_intake_parser(subparsers):
    parser = subparsers.add_parser(
        "intake",
        help="Create a rough task from a freeform brain dump using an LLM",
    )
    parser.add_argument(
        "file",
        type=Path,
        nargs="?",
        help="File containing the brain dump; omit to read from stdin",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        default="opencode",
        help="Engine to use for analysis",
    )
    parser.add_argument(
        "--model",
        help="Model override for the selected engine",
    )
    add_workspace_argument(parser)
