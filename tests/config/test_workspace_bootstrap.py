from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time

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


def _hold_registry_write_lock(
    args: tuple[str, str, str, str, str, float, int, int, int],
) -> None:
    (
        workspace_root,
        config_home,
        data_home,
        state_home,
        ready_path,
        hold_seconds,
        busy_timeout_ms,
        lock_retries,
        lock_retry_delay_ms,
    ) = args
    os.environ["XDG_CONFIG_HOME"] = config_home
    os.environ["XDG_DATA_HOME"] = data_home
    os.environ["XDG_STATE_HOME"] = state_home
    os.environ["LITEHIVE_REGISTRY_BUSY_TIMEOUT_MS"] = str(busy_timeout_ms)
    os.environ["LITEHIVE_REGISTRY_LOCK_RETRIES"] = str(lock_retries)
    os.environ["LITEHIVE_REGISTRY_LOCK_RETRY_DELAY_MS"] = str(lock_retry_delay_ms)

    from litehive.config.paths import litehive_database_path, workspace_id
    from litehive.domain.common import utcnow

    root = Path(workspace_root)
    ensure_workspace(root)
    with sqlite3.connect(litehive_database_path(), timeout=30) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO workspaces (workspace_id, path, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                path = excluded.path,
                last_seen = excluded.last_seen
            """,
            (workspace_id(root), str(root.resolve()), utcnow()),
        )
        Path(ready_path).write_text("ready", encoding="utf-8")
        time.sleep(hold_seconds)
        connection.commit()


def _legacy_registry_path(config_home: Path) -> Path:
    return ((config_home / "litehive") / "workspaces").with_suffix(".yaml")


def _registered_paths(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        return [
            str(row[0])
            for row in connection.execute("SELECT path FROM workspaces ORDER BY last_seen DESC, path ASC").fetchall()
        ]


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
        global_config_path,
        litehive_database_path,
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
    assert global_config_path() == config_home / "litehive" / "config.yaml"
    with sqlite3.connect(litehive_database_path()) as connection:
        columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(workspaces)").fetchall()]
        rows = connection.execute("SELECT workspace_id, path, last_seen FROM workspaces").fetchall()
    assert columns == ["workspace_id", "path", "last_seen"]
    assert len(rows) == 1
    assert str(rows[0][0]) == wid
    assert Path(str(rows[0][1])) == tmp_path.resolve()
    assert str(rows[0][2])

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

    from litehive.config.paths import litehive_database_path

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
    registry_paths = _registered_paths(litehive_database_path())
    assert set(registry_paths) == {str(root.resolve()) for root in workspaces}
    assert len(registry_paths) == len(workspaces)


def test_workspace_registry_retries_lock_contention_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("LITEHIVE_REGISTRY_BUSY_TIMEOUT_MS", "50")
    monkeypatch.setenv("LITEHIVE_REGISTRY_LOCK_RETRIES", "20")
    monkeypatch.setenv("LITEHIVE_REGISTRY_LOCK_RETRY_DELAY_MS", "25")

    from litehive.config.paths import litehive_database_path

    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    workspace_one.mkdir()
    workspace_two.mkdir()

    ready_path = tmp_path / "registry-lock-ready"
    context = multiprocessing.get_context("spawn")
    holder = context.Process(
        target=_hold_registry_write_lock,
        args=(
            (
                str(workspace_one),
                str(config_home),
                str(data_home),
                str(state_home),
                str(ready_path),
                0.4,
                50,
                20,
                25,
            ),
        ),
    )
    holder.start()
    try:
        for _ in range(100):
            if ready_path.exists():
                break
            time.sleep(0.01)
        else:
            pytest.fail("timed out waiting for registry lock holder to acquire the write lock")

        ensure_workspace(workspace_two)
    finally:
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=1)

    assert holder.exitcode == 0
    assert set(_registered_paths(litehive_database_path())) == {
        str(workspace_one.resolve()),
        str(workspace_two.resolve()),
    }


def test_workspace_registry_migrates_legacy_yaml_and_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import litehive_database_path
    from litehive.config.registry import list_registered_workspace_paths

    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    legacy_path = _legacy_registry_path(config_home)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        yaml.safe_dump(
            [str(workspace_two), str(workspace_one), str(workspace_one)],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    migrated = list_registered_workspace_paths()

    assert set(migrated) == {workspace_one.resolve(), workspace_two.resolve()}
    assert not legacy_path.exists()
    assert set(_registered_paths(litehive_database_path())) == {
        str(workspace_one.resolve()),
        str(workspace_two.resolve()),
    }


def test_workspace_registry_rebuilds_after_corruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import litehive_database_path

    litehive_database_path().parent.mkdir(parents=True, exist_ok=True)
    litehive_database_path().write_bytes(b"not a sqlite database")

    ensure_workspace(tmp_path)

    assert _registered_paths(litehive_database_path()) == [str(tmp_path.resolve())]


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
    config_home = tmp_path / "xdg-config"
    monkeypatch.setenv("LITEHIVE_HOME", str(custom_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-state"))

    from litehive.config.paths import (
        global_config_path,
        litehive_database_path,
        litehive_root,
        workspace_database_path,
        workspace_id,
    )

    ensure_workspace(tmp_path)

    wid = workspace_id(tmp_path)
    assert litehive_root() == custom_home
    assert litehive_database_path() == custom_home / "litehive.db"
    assert global_config_path() == config_home / "litehive" / "config.yaml"
    assert workspace_database_path(tmp_path) == custom_home / wid / "data.db"
    assert _registered_paths(litehive_database_path()) == [str(tmp_path.resolve())]


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
