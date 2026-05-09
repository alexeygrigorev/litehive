from pathlib import Path

import pytest

from litehive.config.workspace_files import workspace_dir
from litehive.config.workspace import create_workspace, normalize_workspace_root, resolve_workspace
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace


def test_resolve_workspace_uses_workspace_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_workspace(tmp_path)

    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    assert resolve_workspace(None) == tmp_path.resolve()


def test_resolve_workspace_uses_current_directory_without_searching_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_workspace(tmp_path)

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    assert resolve_workspace(None) == tmp_path.resolve()


def test_resolve_workspace_rejects_subdirectory_without_searching_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_workspace(tmp_path)
    nested = tmp_path / "src"
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    with pytest.raises(ValueError, match="not an existing Litehive project"):
        resolve_workspace(None)


def test_normalize_workspace_root_accepts_plain_root_without_registry_lookup(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path)

    assert normalize_workspace_root(tmp_path, source="test") == tmp_path.resolve()


def test_resolve_workspace_rejects_env_workspace_when_it_does_not_own_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Env task mismatch probe")
    legacy = tmp_path / ".litehive" / "worktrees" / f"{task.id}-bad" / "repo"
    (legacy / ".litehive" / "tasks").mkdir(parents=True)

    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(legacy))
    monkeypatch.setenv("LITEHIVE_TASK_ID", task.id)

    with pytest.raises(ValueError, match=f"task {task.id} is not in"):
        resolve_workspace(None)


def test_resolve_workspace_fails_clearly_when_it_cannot_be_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.chdir(outside)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    with pytest.raises(ValueError, match="not an existing Litehive project"):
        resolve_workspace(None)


def test_create_workspace_rejects_nested_workspace_root(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    nested_root = tmp_path / ".litehive" / "worktrees" / "T-0001"
    nested_root.mkdir(parents=True)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        create_workspace(nested_root)


def test_create_workspace_rejects_litehive_control_directory(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        create_workspace(tmp_path / ".litehive")


def test_create_workspace_rejects_nested_litehive_control_directory(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        create_workspace(tmp_path / ".litehive" / ".litehive")


def test_normalize_workspace_root_only_resolves_nested_control_tree(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    legacy = tmp_path / ".litehive" / "worktrees" / "T-0001-bad" / "repo"
    (legacy / ".litehive" / "tasks").mkdir(parents=True)

    assert normalize_workspace_root(legacy, source="test") == legacy.resolve()


@pytest.mark.parametrize(
    ("target_factory", "message"),
    [
        (lambda root: root / ".litehive", "Litehive control directory"),
        (lambda root: root / ".litehive" / ".litehive", "Litehive control directory"),
        (lambda root: root / ".litehive" / "worktrees" / "T-0001" / "repo", "Litehive control directory"),
    ],
)
def test_create_workspace_rejections_do_not_create_nested_workspace_side_effects(
    tmp_path: Path,
    target_factory,
    message: str,
) -> None:
    create_workspace(tmp_path)
    target = target_factory(tmp_path)
    target.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match=message):
        create_workspace(target)

    assert not workspace_dir(target).exists()


def test_create_workspace_comprehensive_nested_litehive_rejection(tmp_path: Path) -> None:
    """Test comprehensive rejection of nested .litehive directories and edge cases."""
    create_workspace(tmp_path)

    # Test the specific .litehive/.litehive/ case mentioned in the goal
    nested_litehive = tmp_path / ".litehive" / ".litehive"
    nested_litehive.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        create_workspace(nested_litehive)

    # Test deeply nested .litehive case
    deep_nested = tmp_path / ".litehive" / "subdir" / "another" / ".litehive"
    deep_nested.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        create_workspace(deep_nested)

    # Ensure no workspace directories were created in rejected paths
    assert not workspace_dir(nested_litehive).exists()
    assert not workspace_dir(deep_nested).exists()
