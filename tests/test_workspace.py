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


def test_workspace_exposes_bound_control_files(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    workspace = Workspace.from_path(tmp_path)
    control_files = workspace.control_files()

    assert control_files.root == workspace.root
    assert control_files.directory() == workspace.root / ".litehive"
    assert workspace.control_dir() == control_files.directory()
    assert control_files.config() == workspace.root / ".litehive" / "config.yaml"
    assert control_files.context() == workspace.root / ".litehive" / "context.md"
    assert control_files.gitignore() == workspace.root / ".litehive" / ".gitignore"
