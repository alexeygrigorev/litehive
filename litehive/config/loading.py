"""
Config loading and merge helpers.

Owns the layered-config pipeline: defaults from
:class:`LitehiveConfig`, the user-global layer under the litehive
root, and the per-workspace layer. Runtime-setting overrides are
applied last by :meth:`WorkspaceConfigLoader.load` so they always win
over file-based values.
"""

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import yaml
from litehive.config.model import LitehiveConfig, parse_litehive_config_data
from litehive.config.paths import litehive_root

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


class WorkspaceConfigLoader:
    """
    Workspace-bound config and context loader.

    Owns the layered YAML load plus audited runtime-setting overlay for
    a single workspace.
    """

    def __init__(self, workspace: "Workspace") -> None:
        self.workspace = workspace

    def effective_data(self) -> dict[str, Any]:
        """
        Materialize the effective config dict from the layered files.

        Applies defaults, then the user-global layer under the
        litehive root, then the per-workspace layer. Runtime-setting
        overrides are *not* applied here because that step requires
        the SQLite store.
        """
        config = asdict(LitehiveConfig())
        config = merge_config_layers(config, _read_config_layer(litehive_root() / "config.yaml"))
        return merge_config_layers(config, _read_config_layer(self.workspace.control_files().config()))

    def load(self) -> LitehiveConfig:
        """
        Load and validate the workspace config.
        """
        self.workspace.require_existing(source="load_config")
        # inline: runtime_settings transitively pulls db.schema which loads
        # config.* back through litehive/config/__init__.py during partial init.
        from litehive.config.runtime_settings import RuntimeSettingsRepository  # noqa: PLC0415

        data = RuntimeSettingsRepository(self.workspace).apply_to_config_data(self.effective_data())
        return parse_litehive_config_data(data)

    def context(self) -> str:
        """
        Read the workspace context document used as a prompt preamble.

        Requires an existing workspace so reads cannot silently bootstrap a
        new project.
        """
        self.workspace.require_existing(source="load_context")
        return self.workspace.control_files().context().read_text(encoding="utf-8")
