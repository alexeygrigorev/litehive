from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
from pathlib import Path
import threading

import pytest
import yaml

from litehive.config.loading import load_config
from litehive.config.model import (
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    SandboxCredentialInput,
)
from litehive.config.profiles.loader import resolve_process_profile
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task


def _register_workspace_in_subprocess(args: tuple[str, str, str, str]) -> str:
    workspace_root, config_home, data_home, state_home = args
    os.environ["XDG_CONFIG_HOME"] = config_home
    os.environ["XDG_DATA_HOME"] = data_home
    os.environ["XDG_STATE_HOME"] = state_home
    ensure_workspace(Path(workspace_root))
    return workspace_root


def test_ensure_workspace_creates_layout(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    assert (tmp_path / ".litehive" / "config.yaml").exists()
    assert (tmp_path / ".litehive" / ".gitignore").exists()
    assert (tmp_path / ".litehive" / "tasks").exists()


def test_ensure_workspace_bootstraps_runtime_db_and_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import (
        litehive_database_path,
        workspace_registry_path,
        worktree_root,
        workspace_backups_dir,
        workspace_database_path,
        workspace_id,
        workspace_logs_dir,
        workspace_subagents_dir,
        workspace_worktrees_dir,
    )
    from litehive.db.schema import connect_workspace_db

    ensure_workspace(tmp_path)

    wid = workspace_id(tmp_path)
    assert workspace_database_path(tmp_path) == data_home / "litehive" / wid / "data.db"
    assert workspace_backups_dir(tmp_path) == data_home / "litehive" / wid / "backups"
    assert workspace_logs_dir(tmp_path) == data_home / "litehive" / wid / "logs"
    assert workspace_worktrees_dir(tmp_path) == data_home / "litehive" / wid / "worktrees"
    assert worktree_root(tmp_path) == data_home / "litehive" / wid / "worktrees"
    assert workspace_subagents_dir(tmp_path, "T-0001", "agent-1") == (
        data_home / "litehive" / wid / "subagents" / "T-0001" / "agent-1"
    )
    assert workspace_database_path(tmp_path).exists()
    assert litehive_database_path() == data_home / "litehive" / "litehive.db"
    assert workspace_registry_path() == config_home / "litehive" / "workspaces.yaml"

    registry_payload = yaml.safe_load(workspace_registry_path().read_text(encoding="utf-8"))
    assert registry_payload == {"workspaces": [str(tmp_path.resolve())]}

    with connect_workspace_db(tmp_path) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert {
        "schema_migrations",
        "pool_state",
        "queue",
        "task_state",
        "task_journal",
        "stage_reports",
        "hook_artifacts",
        "subagent_sessions",
        "events",
        "engine_monitoring",
        "attention",
        "worktrees",
        "pipeline_transitions",
        "pipeline_journal",
        "pipeline_task_state",
        "pipeline_sessions",
    } <= tables


def test_workspace_registry_handles_parallel_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import workspace_registry_path

    workspaces = []
    for index in range(8):
        root = tmp_path / f"workspace-{index}"
        root.mkdir()
        workspaces.append(root)

    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as executor:
        results = list(
            executor.map(
                _register_workspace_in_subprocess,
                [(str(root), str(config_home), str(data_home), str(state_home)) for root in workspaces],
            )
        )

    assert {Path(path) for path in results} == set(workspaces)
    registry_payload = yaml.safe_load(workspace_registry_path().read_text(encoding="utf-8")) or {}
    assert set(registry_payload["workspaces"]) == {str(root.resolve()) for root in workspaces}
    assert len(registry_payload["workspaces"]) == len(workspaces)


def test_workspace_registry_rebuilds_after_corruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import workspace_registry_path

    workspace_registry_path().parent.mkdir(parents=True, exist_ok=True)
    workspace_registry_path().write_text("not: [valid", encoding="utf-8")

    ensure_workspace(tmp_path)

    registry_payload = yaml.safe_load(workspace_registry_path().read_text(encoding="utf-8")) or {}
    assert registry_payload == {"workspaces": [str(tmp_path.resolve())]}


def test_workspace_registry_is_available_from_other_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    ensure_workspace(tmp_path)

    from litehive.config.registry import list_registered_workspace_paths

    results: list[list[Path]] = []

    def worker() -> None:
        ensure_workspace(tmp_path)
        results.append(list_registered_workspace_paths())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert results == [[tmp_path.resolve()]]


def test_ensure_workspace_skips_task_yaml_rescan_when_runtime_state_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Current runtime state")

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("current runtime state should skip task.yaml rescan")

    monkeypatch.setattr("litehive.state.store.RuntimeStore._seed_task_state_rows_from_disk", _boom)

    ensure_workspace(tmp_path)


def test_ensure_workspace_skips_disk_scan_for_bootstrapped_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("bootstrapped empty workspace should skip disk scan")

    monkeypatch.setattr("litehive.state.store.RuntimeStore._seed_task_state_rows_from_disk", _boom)

    ensure_workspace(tmp_path)


def test_litehive_home_overrides_default_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom_home = tmp_path / "custom-home"
    monkeypatch.setenv("LITEHIVE_HOME", str(custom_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-state"))

    from litehive.config.paths import (
        litehive_database_path,
        litehive_root,
        workspace_registry_path,
        workspace_database_path,
        workspace_id,
    )

    ensure_workspace(tmp_path)

    wid = workspace_id(tmp_path)
    assert litehive_root() == custom_home
    assert litehive_database_path() == custom_home / "litehive.db"
    assert workspace_registry_path() == custom_home / "workspaces.yaml"
    assert workspace_database_path(tmp_path) == custom_home / wid / "data.db"


def test_load_config_round_trips_external_engine_sandbox(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            external_engine_sandbox=ExternalEngineSandboxConfig(
                enabled=True,
                image="ghcr.io/example/litehive-sandbox:latest",
                runtime_args=["--pull=never"],
                engine_policies={
                    "codex": ExternalEngineSandboxPolicy(
                        enabled=True,
                        network_mode="none",
                        workspace_mode="rw",
                        environment=["OPENAI_API_KEY"],
                        extra_ro_binds=["/opt/runtime"],
                        credential_inputs=[
                            SandboxCredentialInput(
                                env_var="GOOGLE_APPLICATION_CREDENTIALS",
                                mount_path="/run/credentials/google.json",
                            )
                        ],
                    )
                },
            )
        ),
    )

    config = load_config(tmp_path)

    assert config.external_engine_sandbox.enabled is True
    assert config.external_engine_sandbox.image == "ghcr.io/example/litehive-sandbox:latest"
    assert config.external_engine_sandbox.runtime_args == ["--pull=never"]
    policy = config.external_engine_sandbox.engine_policies["codex"]
    assert policy.enabled is True
    assert policy.network_mode == "none"
    assert policy.workspace_mode == "rw"
    assert policy.environment == ["OPENAI_API_KEY"]
    assert policy.extra_ro_binds == ["/opt/runtime"]
    assert [item.env_var for item in policy.credential_inputs] == ["GOOGLE_APPLICATION_CREDENTIALS"]


def test_resolve_process_profile_merges_shared_process_with_overlay() -> None:
    profile = resolve_process_profile("codehive")

    assert profile["label"] == "Codehive-style"
    assert profile["shared_stages"] == [
        "grooming",
        "implementing",
        "testing",
        "accepting",
        "commit_to_git",
    ]
    assert profile["orchestrator_model"]
    assert profile["routing_model"]
    assert profile["role_model"]
    assert profile["prompt_scaffold"]
    assert profile["stage_overlay"]["accepting"]
