"""
Config loading and merge helpers.

Owns the layered-config pipeline: defaults from
:class:`LitehiveConfig`, the user-global layer under the litehive
root, and the per-workspace layer. Runtime-setting overrides are
applied last by :func:`load_config` so they always win over
file-based values.
"""

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import yaml
from litehive.config.model import LitehiveConfig, parse_litehive_config_data
from litehive.config.paths import litehive_root
from litehive.config.workspace_files import config_path, context_path

if TYPE_CHECKING:
    from litehive.workspace import Workspace


def _read_config_layer(path: Path) -> dict[str, Any]:
    """
    Load one YAML config layer.

    Missing files return ``{}`` so every caller can blindly merge
    over the default-config baseline without testing for absence.
    Non-mapping payloads raise loudly because the contract is "a
    config file is a YAML mapping" — silently turning a list or
    scalar into ``{}`` would mask the operator's mistake.
    """
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return dict(payload)


def merge_config_layers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """
    Deep-merge ``overlay`` over ``base``.

    Nested mappings combine recursively; scalars and lists are
    replaced wholesale because partial list merging would force
    operators to specify positional intent. Defines the precedence
    rule between defaults, user-global, and workspace config
    layers.
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = merge_config_layers(current, value)
        else:
            merged[key] = value
    return merged


def load_effective_config_data(root: Path) -> dict[str, Any]:
    """
    Materialize the effective config dict from the layered files.

    Applies defaults, then the user-global layer under the
    litehive root, then the per-workspace layer. Runtime-setting
    overrides are *not* applied here because that step requires
    the SQLite store; tests and tools that only want the file
    layout call this directly.
    """
    config = asdict(LitehiveConfig())
    config = merge_config_layers(config, _read_config_layer(litehive_root() / "config.yaml"))
    return merge_config_layers(config, _read_config_layer(config_path(root)))


def load_config(root: Path) -> LitehiveConfig:
    """
    Public config entrypoint.

    Path-based compatibility wrapper. Callers that already have a
    :class:`Workspace` should use ``workspace.load_config()`` so
    config loading does not rebuild workspace dependencies.
    """
    from litehive.workspace import Workspace  # noqa: PLC0415

    return load_config_for_workspace(Workspace.from_path(root))


def load_config_for_workspace(workspace: "Workspace") -> LitehiveConfig:
    """
    Load config for an injected workspace.

    Requires an existing workspace, layers in runtime-setting overrides
    on top of the file-based config, and returns a validated
    :class:`LitehiveConfig`. Workspace creation stays explicit through
    ``create_workspace``.
    """
    root = workspace.require_existing(source="load_config")
    # inline: runtime_settings transitively pulls db.schema which loads
    # config.* back through litehive/config/__init__.py during partial init.
    from litehive.config.runtime_settings import apply_runtime_settings_to_config_data  # noqa: PLC0415

    data = apply_runtime_settings_to_config_data(workspace, load_effective_config_data(root))
    return parse_litehive_config_data(data)


def load_context(root: Path) -> str:
    """
    Read the workspace context document used as a prompt preamble.

    The context document is the stable workspace-level prose
    every agent prompt opens with (project background, conventions).
    Requires an existing workspace so reads cannot silently bootstrap a
    new project.
    """
    from litehive.config.workspace import require_existing_workspace  # noqa: PLC0415

    workspace_root = require_existing_workspace(root, source="load_context")
    return context_path(workspace_root).read_text(encoding="utf-8")
