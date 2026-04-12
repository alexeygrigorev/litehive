from datetime import UTC, datetime
import yaml
from litehive.agents import ENGINE_CHOICES, get_engine
from litehive.config import config_path, ensure_workspace, load_config
def _config(root):
    ensure_workspace(root)
    path = config_path(root)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return load_config(root), path, data if isinstance(data, dict) else {}
def cmd_engine(args):
    if args.engine_action == "status":
        if getattr(args, "engine_name", None):
            print("engine status: does not take an engine name")
            return 1
        config, _, _ = _config(args.workspace)
        frozen = ", ".join(f"{k}={v}" for k, v in sorted(config.engine_freeze.items())) or "-"
        engines = ", ".join(
            f"{name}(available={'yes' if c.available else 'no'}, model_override={'yes' if c.supports_model_override else 'no'}, strips_env={'yes' if c.strips_environment else 'no'})"
            for name in ENGINE_CHOICES
            for c in [get_engine(name).capabilities]
        )
        print(f"default_engine: {config.default_engine} | engine_freeze: {frozen} | engines: {engines}")
        return 0
    name = getattr(args, "engine_name", None)
    if name not in ENGINE_CHOICES:
        print(f"engine {args.engine_action}: unknown engine '{name}'")
        return 1
    _, path, raw = _config(args.workspace)
    frozen = raw.get("engine_freeze") if isinstance(raw.get("engine_freeze"), dict) else {}
    if args.engine_action == "freeze":
        try:
            until = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            print("engine freeze: --until must be ISO date YYYY-MM-DD")
            return 1
        raw["engine_freeze"] = frozen | {name: until}
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        print(f"engine_frozen: {name} until {until}" + (f" reason={args.reason}" if getattr(args, "reason", None) else ""))
        return 0
    if name not in frozen:
        print(f"engine unfreeze: {name} is not frozen")
        return 1
    frozen.pop(name)
    raw["engine_freeze"] = frozen
    raw.pop("engine_freeze", None) if not frozen else None
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    print(f"engine_unfrozen: {name}")
    return 0
