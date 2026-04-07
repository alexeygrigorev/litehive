from datetime import datetime, timezone

from litehive.config import load_config, ensure_workspace, config_path
from litehive.engines import ENGINE_CHOICES

import yaml


def _parse_local_datetime(value: str) -> datetime:
    """Parse a date or datetime string as local timezone, return UTC datetime."""
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            local_dt = datetime.strptime(value, fmt)
            local_dt = local_dt.astimezone()  # attach local timezone
            return local_dt.astimezone(timezone.utc)
        except ValueError:
            continue
    # Date-only: treat as start of day in local timezone
    try:
        local_dt = datetime.strptime(value, "%Y-%m-%d")
        local_dt = local_dt.astimezone()  # attach local timezone
        return local_dt.astimezone(timezone.utc)
    except ValueError:
        pass
    raise ValueError(
        f"Cannot parse '{value}' as date or datetime. "
        "Expected format: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'"
    )


def _read_raw_config(path):
    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw_data, dict):
        return None
    return raw_data


def _write_raw_config(path, raw_data):
    path.write_text(yaml.safe_dump(raw_data, sort_keys=False), encoding="utf-8")


def _resolve_engine_action(args):
    """Resolve the engine subcommand and engine name from args.

    Supports both:
      litehive engine codex          (backward compat, treated as 'set codex')
      litehive engine set codex
      litehive engine freeze codex --until ...
      litehive engine unfreeze codex
    """
    action = args.engine_action
    engine_name = getattr(args, "engine_name", None)

    if action in ("set", "freeze", "unfreeze"):
        if engine_name is None:
            return action, None  # will be caught as error in handler
        if engine_name not in ENGINE_CHOICES:
            return action, None
        return action, engine_name

    # Backward compat: `litehive engine codex` -> set codex
    if action in ENGINE_CHOICES:
        return "set", action

    return action, engine_name


def _cmd_engine(args):
    # Backward compat: old-style args have args.engine instead of engine_action
    if hasattr(args, "engine") and not hasattr(args, "engine_action"):
        args.engine_action = args.engine
        args.engine_name = None

    action, engine_name = _resolve_engine_action(args)

    if action == "set":
        return _cmd_engine_set(args.workspace, engine_name)
    if action == "freeze":
        until = getattr(args, "until", None)
        return _cmd_engine_freeze(args.workspace, engine_name, until)
    if action == "unfreeze":
        return _cmd_engine_unfreeze(args.workspace, engine_name)

    print(f"engine: unknown subcommand '{action}'")
    return 1


def _cmd_engine_set(workspace, engine_name):
    if engine_name is None:
        print("engine set: engine name required")
        return 1
    ensure_workspace(workspace)
    config = load_config(workspace)
    path = config_path(workspace)
    raw_data = _read_raw_config(path)
    if raw_data is None:
        print(f"engine failed: workspace config must be a mapping: {path}")
        return 1

    previous_engine = config.default_engine
    raw_data["default_engine"] = engine_name
    _write_raw_config(path, raw_data)

    print(f"workspace: {workspace}")
    print(f"default_engine: {previous_engine} -> {engine_name}")
    print(f"config: {path}")
    return 0


def _cmd_engine_freeze(workspace, engine_name, until_str):
    if engine_name is None:
        print("engine freeze: engine name required")
        return 1
    if until_str is None:
        print("engine freeze: --until is required")
        return 1
    ensure_workspace(workspace)
    path = config_path(workspace)
    raw_data = _read_raw_config(path)
    if raw_data is None:
        print(f"engine failed: workspace config must be a mapping: {path}")
        return 1

    try:
        freeze_utc = _parse_local_datetime(until_str)
    except ValueError as exc:
        print(f"engine freeze: {exc}")
        return 1

    freeze_map = raw_data.get("engine_freeze", {})
    if not isinstance(freeze_map, dict):
        freeze_map = {}
    freeze_map[engine_name] = freeze_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_data["engine_freeze"] = freeze_map
    _write_raw_config(path, raw_data)

    local_display = freeze_utc.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    print(f"workspace: {workspace}")
    print(f"engine_frozen: {engine_name} until {local_display}")
    print(f"config: {path}")
    return 0


def _cmd_engine_unfreeze(workspace, engine_name):
    if engine_name is None:
        print("engine unfreeze: engine name required")
        return 1
    ensure_workspace(workspace)
    path = config_path(workspace)
    raw_data = _read_raw_config(path)
    if raw_data is None:
        print(f"engine failed: workspace config must be a mapping: {path}")
        return 1

    freeze_map = raw_data.get("engine_freeze", {})
    if not isinstance(freeze_map, dict):
        freeze_map = {}
    if engine_name not in freeze_map:
        print(f"engine unfreeze: {engine_name} is not frozen")
        return 1

    del freeze_map[engine_name]
    if freeze_map:
        raw_data["engine_freeze"] = freeze_map
    else:
        raw_data.pop("engine_freeze", None)
    _write_raw_config(path, raw_data)

    print(f"workspace: {workspace}")
    print(f"engine_unfrozen: {engine_name}")
    print(f"config: {path}")
    return 0
