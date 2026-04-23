from pathlib import Path
from typing import Annotated

import typer
import yaml
from heru import ENGINE_CHOICES, get_engine

from litehive.cli.common import WorkspaceOption, choice
from litehive.config.engine_models import parse_engine_freeze_until
from litehive.config.loading import load_config
from litehive.config.workspace import ensure_workspace
from litehive.config.workspace_files import config_path
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.state.persist import load_state
from litehive.tasks.status import switch_task_engine


def _config(root: Path) -> tuple[object, Path, dict]:
    root = root.resolve()
    config = load_config(root)
    path = config_path(root)
    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return config, path, raw_data if isinstance(raw_data, dict) else {}


def _switch_task_engine_command(
    workspace: Path,
    *,
    task_id: str | None,
    engine_name: str | None,
    reason: str | None,
) -> int:
    ensure_workspace(workspace)
    if not task_id:
        print("engine switch: task id is required")
        return 1
    if not engine_name:
        print("engine switch: engine name is required")
        return 1
    try:
        summary = switch_task_engine(workspace, task_id, engine=engine_name, reason=reason or "")
    except (ValueError, WorkspaceConflictError) as exc:
        print(f"engine switch failed: {exc}")
        return 1
    state = load_state(workspace)
    print(f"task: {summary.task.id} {summary.task.title}")
    print("status: queued")
    print(f"pipeline_stage: {summary.task.pipeline_status}")
    print(f"pipeline_status: {summary.task.pipeline_status}")
    print(f"engine: {summary.previous_engine} -> {summary.new_engine}")
    print(f"was_active: {'yes' if summary.was_active else 'no'}")
    print(f"runner_pid: {summary.runner_pid if summary.runner_pid is not None else '-'}")
    print(f"signal_sent: {'yes' if summary.signal_sent else 'no'}")
    print(f"position: {state.queue.index(summary.task.id) + 1}")
    return 0


def engine_command(
    workspace: WorkspaceOption = Path.cwd(),
    action: Annotated[
        str,
        typer.Argument(click_type=choice(["freeze", "status", "switch", "unfreeze"]), help="Subcommand"),
    ] = ...,
    target: Annotated[
        str | None,
        typer.Argument(help="Engine name for freeze/unfreeze, or task id for switch"),
    ] = None,
    engine_name: Annotated[
        str | None,
        typer.Argument(help="Engine name for switch"),
    ] = None,
    until: Annotated[str | None, typer.Option(help="Freeze until this ISO date (YYYY-MM-DD)")] = None,
    reason: Annotated[str | None, typer.Option(help="Operator note; required for switch")] = None,
) -> int:
    config, path, data = _config(workspace)
    if action == "status":
        if target or engine_name:
            print("engine status: does not take positional arguments")
            return 1
        frozen = ", ".join(f"{k}={v}" for k, v in sorted(config.engine_freeze.items())) or "-"
        engines = ", ".join(
            (
                f"{name}(available={'yes' if caps.available else 'no'}, "
                f"model_override={'yes' if caps.supports_model_override else 'no'}, "
                f"strips_env={'yes' if caps.strips_environment else 'no'})"
            )
            for name in ENGINE_CHOICES
            for caps in [get_engine(name).capabilities]
        )
        print(f"default_engine: {config.default_engine} | engine_freeze: {frozen} | engines: {engines}")
        return 0
    if action == "switch":
        return _switch_task_engine_command(
            workspace,
            task_id=target,
            engine_name=engine_name,
            reason=reason,
        )

    if engine_name is not None:
        print(f"engine {action}: unexpected extra argument '{engine_name}'")
        return 1
    if target not in ENGINE_CHOICES:
        print(f"engine {action}: unknown engine '{target}'")
        return 1

    freeze_map = dict(data.get("engine_freeze")) if isinstance(data.get("engine_freeze"), dict) else {}
    if action == "freeze":
        freeze_iso = parse_engine_freeze_until(until)
        if freeze_iso is None:
            print("engine freeze: --until must be ISO date YYYY-MM-DD")
            return 1
        freeze_map[target] = freeze_iso
        data["engine_freeze"] = freeze_map
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        suffix = f" reason={reason}" if reason else ""
        print(f"engine_frozen: {target} until {freeze_iso}{suffix}")
        return 0

    if target not in freeze_map:
        print(f"engine unfreeze: {target} is not frozen")
        return 1
    freeze_map.pop(target)
    if freeze_map:
        data["engine_freeze"] = freeze_map
    else:
        data.pop("engine_freeze", None)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    print(f"engine_unfrozen: {target}")
    return 0
