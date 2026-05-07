from dataclasses import is_dataclass
from pathlib import Path

from litehive.config.workspace import create_workspace
from litehive.workspace import Workspace


def test_workspace_is_normal_class_with_root_identity(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    first = Workspace.from_path(tmp_path)
    second = Workspace.from_path(tmp_path)

    assert not is_dataclass(first)
    assert first == second
    assert hash(first) == hash(second)
    assert repr(first) == f"Workspace(root={tmp_path.resolve()!r})"
