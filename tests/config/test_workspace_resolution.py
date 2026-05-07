from pathlib import Path
import threading

import pytest

from litehive.config.paths import workspace_path
from litehive.config.workspace_files import workspace_dir
from litehive.config.workspace import ensure_workspace, normalize_workspace_root, resolve_workspace
from litehive.state.records import create_task


def test_resolve_workspace_uses_workspace_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)

    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    assert resolve_workspace(None) == tmp_path.resolve()


def test_resolve_workspace_walks_up_and_normalizes_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Walk up worktree")

    nested = workspace_path(tmp_path, "worktrees") / task.id / "src"
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)
    monkeypatch.setenv("LITEHIVE_TASK_ID", task.id)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)

    assert resolve_workspace(None) == tmp_path.resolve()


def test_resolve_workspace_prefers_current_unified_root_worktree_over_registry_task_id_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    ensure_workspace(workspace_one)
    ensure_workspace(workspace_two)
    task_one = create_task(workspace_one, title="first task")
    task_two = create_task(workspace_two, title="second task")

    assert task_one.id == task_two.id == "T-0001"

    nested = workspace_path(workspace_two, "worktrees") / task_two.id / "src"
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)
    monkeypatch.setenv("LITEHIVE_TASK_ID", task_two.id)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)

    assert resolve_workspace(None) == workspace_two.resolve()


def test_normalize_workspace_root_accepts_plain_root_without_registry_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    from litehive.config.registry import WorkspaceRegistry, build_workspace_registry

    registry = build_workspace_registry(path=tmp_path / "registry.db", mutex=threading.RLock())

    def _boom(self: WorkspaceRegistry) -> list[Path]:
        raise AssertionError("plain workspace roots should not scan the registry")

    monkeypatch.setattr(WorkspaceRegistry, "list_paths", _boom)

    assert normalize_workspace_root(tmp_path, source="test", registry=registry) == tmp_path.resolve()


def test_resolve_workspace_uses_registry_from_outside_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Registry lookup")
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.chdir(outside)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    assert resolve_workspace(task.id) == tmp_path.resolve()


def test_resolve_workspace_uses_injected_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from litehive.config.registry import build_workspace_registry

    registry = build_workspace_registry(path=tmp_path / "registry.db", mutex=threading.RLock())
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    ensure_workspace(workspace, registry=registry)
    task = create_task(workspace, title="Injected registry")

    monkeypatch.chdir(outside)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    assert resolve_workspace(task.id, registry=registry) == workspace.resolve()


def test_resolve_workspace_rejects_nested_workspace_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Nested env probe")
    legacy = tmp_path / ".litehive" / "worktrees" / f"{task.id}-bad" / "repo"
    (legacy / ".litehive" / "tasks").mkdir(parents=True)

    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", str(legacy))
    monkeypatch.setenv("LITEHIVE_TASK_ID", task.id)

    with pytest.raises(ValueError, match="choose the real repo root"):
        resolve_workspace(None)


def test_resolve_workspace_fails_clearly_when_it_cannot_be_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.chdir(outside)
    monkeypatch.delenv("LITEHIVE_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    with pytest.raises(ValueError, match="provide/set LITEHIVE_TASK_ID"):
        resolve_workspace(None)


def test_ensure_workspace_rejects_nested_workspace_root(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    nested_root = tmp_path / ".litehive" / "worktrees" / "T-0001"
    nested_root.mkdir(parents=True)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        ensure_workspace(nested_root)


def test_ensure_workspace_rejects_litehive_control_directory(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        ensure_workspace(tmp_path / ".litehive")


def test_ensure_workspace_rejects_nested_litehive_control_directory(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        ensure_workspace(tmp_path / ".litehive" / ".litehive")


def test_normalize_workspace_root_rejects_nested_control_tree(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    legacy = tmp_path / ".litehive" / "worktrees" / "T-0001-bad" / "repo"
    (legacy / ".litehive" / "tasks").mkdir(parents=True)

    with pytest.raises(ValueError, match="choose the real repo root"):
        normalize_workspace_root(legacy, source="test")


@pytest.mark.parametrize(
    ("target_factory", "message"),
    [
        (lambda root: root / ".litehive", "Litehive control directory"),
        (lambda root: root / ".litehive" / ".litehive", "Litehive control directory"),
        (lambda root: root / ".litehive" / "worktrees" / "T-0001" / "repo", "Litehive control directory"),
    ],
)
def test_ensure_workspace_rejections_do_not_create_nested_workspace_side_effects(
    tmp_path: Path,
    target_factory,
    message: str,
) -> None:
    ensure_workspace(tmp_path)
    target = target_factory(tmp_path)
    target.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match=message):
        ensure_workspace(target)

    assert not workspace_dir(target).exists()


def test_ensure_workspace_comprehensive_nested_litehive_rejection(tmp_path: Path) -> None:
    """Test comprehensive rejection of nested .litehive directories and edge cases."""
    ensure_workspace(tmp_path)

    # Test the specific .litehive/.litehive/ case mentioned in the goal
    nested_litehive = tmp_path / ".litehive" / ".litehive"
    nested_litehive.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        ensure_workspace(nested_litehive)

    # Test deeply nested .litehive case
    deep_nested = tmp_path / ".litehive" / "subdir" / "another" / ".litehive"
    deep_nested.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="Litehive control directory.*choose the real repo root"):
        ensure_workspace(deep_nested)

    # Ensure no workspace directories were created in rejected paths
    assert not workspace_dir(nested_litehive).exists()
    assert not workspace_dir(deep_nested).exists()
