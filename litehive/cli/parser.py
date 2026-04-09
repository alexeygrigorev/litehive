import argparse

from litehive.cli.parsers import COMMAND_PARSER_BUILDERS


def build_parser():
    parser = argparse.ArgumentParser(prog="litehive")
    subparsers = parser.add_subparsers(dest="command")
    for register_parser in COMMAND_PARSER_BUILDERS:
        register_parser(subparsers)
    return parser
