from pathlib import Path
from typing import Annotated

import typer
import yaml
from heru import ENGINE_CHOICES, get_engine

from litehive.cli.common import WorkspaceOption, choice
from litehive.config.engine_models import parse_engine_freeze_until
from litehive.config.loading import load_config
from litehive.config.workspace_files import config_path

def _config(root: Path):
    root = root.resolve(); config = load_config(root); path = config_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return config, path, data if isinstance(data, dict) else {}

def engine_command(
    workspace: WorkspaceOption = Path.cwd(),
    action: Annotated[str, typer.Argument(click_type=choice(["freeze", "unfreeze", "status"]), help="Subcommand")] = ...,
    name: Annotated[str | None, typer.Argument(help="Engine name")] = None,
    until: Annotated[str | None, typer.Option(help="Freeze until this ISO date (YYYY-MM-DD)")] = None,
    reason: Annotated[str | None, typer.Option(help="Optional operator note")] = None,
) -> int:
    config, path, data = _config(workspace)
    if action == "status":
        if name: print("engine status: does not take an engine name"); return 1
        frozen = ", ".join(f"{k}={v}" for k, v in sorted(config.engine_freeze.items())) or "-"
        engines = ", ".join(f"{n}(available={'yes' if c.available else 'no'}, model_override={'yes' if c.supports_model_override else 'no'}, strips_env={'yes' if c.strips_environment else 'no'})" for n in ENGINE_CHOICES for c in [get_engine(n).capabilities])
        print(f"default_engine: {config.default_engine} | engine_freeze: {frozen} | engines: {engines}")
        return 0
    if name not in ENGINE_CHOICES: print(f"engine {action}: unknown engine '{name}'"); return 1
    freeze_map = dict(data.get("engine_freeze")) if isinstance(data.get("engine_freeze"), dict) else {}
    if action == "freeze":
        freeze_iso = parse_engine_freeze_until(until)
        if freeze_iso is None: print("engine freeze: --until must be ISO date YYYY-MM-DD"); return 1
        freeze_map[name] = freeze_iso; data["engine_freeze"] = freeze_map
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        print(f"engine_frozen: {name} until {freeze_iso}" + (f" reason={reason}" if reason else ""))
        return 0
    if name not in freeze_map: print(f"engine unfreeze: {name} is not frozen"); return 1
    freeze_map.pop(name)
    if freeze_map: data["engine_freeze"] = freeze_map
    else: data.pop("engine_freeze", None)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    print(f"engine_unfrozen: {name}")
    return 0
