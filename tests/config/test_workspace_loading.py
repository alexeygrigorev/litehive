from pathlib import Path

import pytest

from litehive.config.loading import load_config_for_workspace, load_context_for_workspace
from litehive.workspace import Workspace


def test_load_config_rejects_non_workspace_without_bootstrapping(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an existing Litehive project"):
        load_config_for_workspace(Workspace.from_path(tmp_path))

    assert not (tmp_path / ".litehive").exists()


def test_load_context_rejects_non_workspace_without_bootstrapping(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an existing Litehive project"):
        load_context_for_workspace(Workspace.from_path(tmp_path))

    assert not (tmp_path / ".litehive").exists()
