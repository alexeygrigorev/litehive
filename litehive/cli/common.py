from pathlib import Path
from typing import Annotated

import click
import typer

WorkspaceOption = Annotated[
    Path,
    typer.Option("--workspace", help="Repository root containing .litehive/"),
]


def make_typer(*, invoke_without_command: bool = False) -> typer.Typer:
    return typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        invoke_without_command=invoke_without_command,
        no_args_is_help=False,
    )


def choice(values: list[str] | tuple[str, ...] | set[str]) -> click.Choice:
    return click.Choice(sorted(values), case_sensitive=True)


def require_subcommand(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help())
    raise typer.Exit(2)
