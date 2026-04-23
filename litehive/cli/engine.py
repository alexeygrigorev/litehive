from pathlib import Path
from typing import Annotated

import typer
from heru import ENGINE_CHOICES, get_engine

from litehive.cli.common import WorkspaceOption, choice
from litehive.config.engine_models import clear_persisted_engine_freeze, parse_engine_freeze_until, persist_engine_freeze_iso
from litehive.config.loading import load_config


def engine_command(
    workspace: WorkspaceOption = Path.cwd(),
    action: Annotated[str, typer.Argument(click_type=choice(["freeze", "status", "unfreeze"]), help="Subcommand")] = ...,
    name: Annotated[str | None, typer.Argument(help="Engine name for freeze/unfreeze")] = None,
    until: Annotated[str | None, typer.Option(help="Freeze until this ISO date (YYYY-MM-DD)")] = None,
    reason: Annotated[str | None, typer.Option(help="Operator note")] = None,
) -> int:
    config = load_config(workspace)
    if action == "status":
        if name:
            print("engine status: does not take positional arguments")
            return 1
        frozen = ", ".join(f"{k}={v}" for k, v in sorted(config.engine_freeze.items())) or "-"
        engines = ", ".join(
            f"{name}(available={'yes' if caps.available else 'no'}, model_override={'yes' if caps.supports_model_override else 'no'}, strips_env={'yes' if caps.strips_environment else 'no'})"
            for name in ENGINE_CHOICES
            for caps in [get_engine(name).capabilities]
        )
        print(f"default_engine: {config.default_engine} | engine_freeze: {frozen} | engines: {engines}")
        return 0
    if name not in ENGINE_CHOICES:
        print(f"engine {action}: unknown engine '{name}'")
        return 1
    if action == "freeze":
        freeze_iso = parse_engine_freeze_until(until)
        if freeze_iso is None:
            print("engine freeze: --until must be ISO date YYYY-MM-DD")
            return 1
        persist_engine_freeze_iso(workspace, engine_name=name, freeze_iso=freeze_iso)
        print(f"engine_frozen: {name} until {freeze_iso}" + (f" reason={reason}" if reason else ""))
        return 0
    if not clear_persisted_engine_freeze(workspace, engine_name=name):
        print(f"engine unfreeze: {name} is not frozen")
        return 1
    print(f"engine_unfrozen: {name}")
    return 0
