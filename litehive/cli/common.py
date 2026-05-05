from pathlib import Path
from typing import Annotated

import click
import typer

WorkspaceOption = Annotated[
    Path,
    typer.Option("--workspace", help="Repository root containing .litehive/"),
]


def make_typer(invoke_without_command: bool = False) -> typer.Typer:
    """Build a Typer app pre-configured with the project's house style (no completion noise, `-h` alongside `--help`, plain markup, no pretty exception traces) so every CLI subgroup looks the same to operators."""
    return typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        invoke_without_command=invoke_without_command,
        no_args_is_help=False,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
    )


def choice(values: list[str] | tuple[str, ...] | set[str]) -> click.Choice:
    """Build a click `Choice` with case-sensitive matching and a deterministic sort, so engine/role validation produces stable error listings regardless of caller-side iteration order."""
    return click.Choice(sorted(values), case_sensitive=True)


def require_subcommand(ctx: typer.Context) -> None:
    """Used as the default callback on subcommand groups that have no useful bare-invocation behavior; prints help and exits 2 so the operator gets guidance instead of a silent no-op."""
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help())
    raise typer.Exit(2)
