from litehive.config import load_config, ensure_workspace, config_path

import yaml


def _cmd_engine(args):
    ensure_workspace(args.workspace)
    config = load_config(args.workspace)
    path = config_path(args.workspace)
    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw_data, dict):
        print(f"engine failed: workspace config must be a mapping: {path}")
        return 1

    previous_engine = config.default_engine
    next_engine = args.engine
    raw_data["default_engine"] = next_engine
    path.write_text(yaml.safe_dump(raw_data, sort_keys=False), encoding="utf-8")

    print(f"workspace: {args.workspace}")
    print(f"default_engine: {previous_engine} -> {next_engine}")
    print(f"config: {path}")
    return 0
