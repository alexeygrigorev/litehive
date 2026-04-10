import argparse

from litehive.cli.parsers import COMMAND_PARSER_BUILDERS

_PUBLIC_TOP_LEVEL_COMMANDS = {
    "configure",
    "status",
    "doctor",
    "health",
    "engine",
    "queue",
    "task",
    "import",
    "repair",
    "web",
    "start",
    "stop",
    "restart",
    "run",
    "rollback",
    "report",
    "worktree",
    "archive",
}

_PUBLIC_GROUP_COMMANDS = {
    "task": {"add", "list", "show", "update", "close", "abandon", "debug", "logs"},
    "queue": {"move", "promote", "requeue", "resume", "stop"},
    "import": {"github", "issue", "spec"},
    "archive": {"cleanup"},
    "worktree": {"ls", "clean", "rescue"},
}


def _subparsers_action(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _filter_public_help(parser: argparse.ArgumentParser, visible: set[str]) -> None:
    action = _subparsers_action(parser)
    if action is None:
        return
    action.metavar = "command"
    action._choices_actions = [
        choice_action
        for choice_action in action._choices_actions
        if choice_action.dest in visible
    ]


def build_parser():
    parser = argparse.ArgumentParser(prog="litehive")
    subparsers = parser.add_subparsers(dest="command")
    for register_parser in COMMAND_PARSER_BUILDERS:
        register_parser(subparsers)
    _filter_public_help(parser, _PUBLIC_TOP_LEVEL_COMMANDS)
    action = _subparsers_action(parser)
    if action is not None:
        for command, visible in _PUBLIC_GROUP_COMMANDS.items():
            child = action.choices.get(command)
            if child is not None:
                _filter_public_help(child, visible)
    return parser
