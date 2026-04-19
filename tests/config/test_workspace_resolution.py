from pathlib import Path

import pytest

from litehive.config.paths import workspace_path
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    def _boom() -> list[Path]:
        raise AssertionError("plain workspace roots should not scan the registry")

    monkeypatch.setattr("litehive.config.workspace.list_registered_workspace_paths", _boom)

    assert normalize_workspace_root(tmp_path, source="test") == tmp_path.resolve()


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


def test_resolve_workspace_rejects_unresolved_workspace_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    monkeypatch.setenv("LITEHIVE_WORKSPACE_ROOT", "$tmpdir/project")
    monkeypatch.delenv("LITEHIVE_TASK_ID", raising=False)

    with pytest.raises(ValueError, match="unresolved shell variable"):
        resolve_workspace(None)


def test_resolve_workspace_rejects_nested_workspace_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    with pytest.raises(ValueError, match="managed worktrees.*choose the real repo root"):
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


def test_ensure_workspace_rejects_nested_subdirectory_of_existing_workspace(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    nested_root = tmp_path / "packages" / "demo"
    nested_root.mkdir(parents=True)

    with pytest.raises(ValueError, match="inside existing Litehive workspace.*nested subdirectory"):
        ensure_workspace(nested_root)


def test_ensure_workspace_rejects_leading_unresolved_shell_var() -> None:
    with pytest.raises(ValueError, match="unresolved shell variable syntax.*expanded absolute path"):
        ensure_workspace(Path("$tmpdir/project"))


def test_ensure_workspace_rejects_embedded_unresolved_shell_var() -> None:
    with pytest.raises(ValueError, match="unresolved shell variable syntax.*expanded absolute path"):
        ensure_workspace(Path("/tmp/$tmpdir/project"))


def test_ensure_workspace_rejects_braced_unresolved_shell_var() -> None:
    with pytest.raises(ValueError, match="unresolved shell variable syntax.*expanded absolute path"):
        ensure_workspace(Path("/tmp/${tmpdir}/project"))
