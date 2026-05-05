from pathlib import Path
from typing import Annotated

import click
import typer

WorkspaceOption = Annotated[
    Path,
    typer.Option("--workspace", help="Repository root containing .litehive/"),
]


def make_typer(invoke_without_command: bool = False) -> typer.Typer:
    """
    Build a Typer app pre-configured with the project's house style.

    Disables completion noise, accepts ``-h`` alongside ``--help``,
    turns off rich markup, and suppresses pretty exception traces so
    every CLI subgroup presents the same surface to operators and
    tests can capture output without ANSI artifacts.
    """
    return typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        invoke_without_command=invoke_without_command,
        no_args_is_help=False,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
    )


def choice(values: list[str] | tuple[str, ...] | set[str]) -> click.Choice:
    """
    Build a click ``Choice`` with case-sensitive matching and deterministic ordering.

    Sorting up front keeps engine/role validation error messages
    stable across runs regardless of caller-side iteration order
    (sets, dict views), which prevents flaky CLI snapshot tests.
    """
    return click.Choice(sorted(values), case_sensitive=True)


def require_subcommand(ctx: typer.Context) -> None:
    """
    Default callback for subcommand groups without a useful bare invocation.

    Prints help and exits 2 so the operator gets guidance instead of
    a silent no-op when they type only the group name. Exit code 2
    signals "usage error" the way most CLI conventions expect.
    """
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help())
    raise typer.Exit(2)
