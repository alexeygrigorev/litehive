from litehive.agents import ENGINE_CHOICES
from litehive.cli.parsers.common import add_workspace_argument


def register_engine_parser(subparsers):
    parser = subparsers.add_parser("engine", help="Manage engine freezes and status")
    engine = parser.add_subparsers(dest="engine_action", required=True)
    freeze = engine.add_parser("freeze", help="Freeze an engine until an ISO date")
    freeze.set_defaults(reason=None)
    freeze.add_argument("engine_name", choices=ENGINE_CHOICES)
    freeze.add_argument("--until", required=True, help="Freeze until this ISO date (YYYY-MM-DD)")
    freeze.add_argument("--reason", default=None, help="Optional operator note echoed in command output")
    add_workspace_argument(freeze)
    unfreeze = engine.add_parser("unfreeze", help="Remove an engine freeze")
    unfreeze.set_defaults(until=None, reason=None)
    unfreeze.add_argument("engine_name", choices=ENGINE_CHOICES)
    add_workspace_argument(unfreeze)
    status = engine.add_parser("status", help="Show compact engine freeze and capability summary")
    status.set_defaults(engine_name=None, until=None, reason=None)
    add_workspace_argument(status)
