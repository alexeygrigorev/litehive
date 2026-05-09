from pathlib import Path

import pytest

from litehive.config.loading import WorkspaceConfigLoader
from litehive.workspace import Workspace


def test_load_config_rejects_non_workspace_without_bootstrapping(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an existing Litehive project"):
        WorkspaceConfigLoader(Workspace.from_path(tmp_path)).load()

    assert not (tmp_path / ".litehive").exists()


def test_load_context_rejects_non_workspace_without_bootstrapping(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an existing Litehive project"):
        WorkspaceConfigLoader(Workspace.from_path(tmp_path)).context()

    assert not (tmp_path / ".litehive").exists()
