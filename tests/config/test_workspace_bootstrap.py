from concurrent.futures import ProcessPoolExecutor
import fcntl
import multiprocessing
import os
from pathlib import Path
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

    root = Path(workspace_root)
    del busy_timeout_ms
    ensure_workspace(root)
    from litehive.config.registry import workspace_registry_path

    lock_path = workspace_registry_path().with_name(".workspaces.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        Path(ready_path).write_text("ready", encoding="utf-8")
        time.sleep(hold_seconds)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _registry_path(config_home: Path) -> Path:
    return config_home / "litehive" / "workspaces.yaml"


def _registered_paths(registry_path: Path) -> list[str]:
    payload = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or []
    return [str(Path(entry).expanduser().resolve()) for entry in payload if isinstance(entry, str)]


def test_ensure_workspace_creates_layout(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    assert (tmp_path / ".litehive" / "config.yaml").exists()
    assert (tmp_path / ".litehive" / ".gitignore").exists()
    assert (tmp_path / ".litehive" / "tasks").exists()
    assert (tmp_path / ".litehive" / "attention").exists()


def test_ensure_workspace_bootstraps_runtime_db_and_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import litehive_root, workspace_data_dir, workspace_path
    from litehive.db.schema import connect_workspace_db

    ensure_workspace(tmp_path)

    wid = workspace_data_dir(tmp_path).name
    assert workspace_path(tmp_path, "data.db") == data_home / "litehive" / wid / "data.db"
    assert workspace_path(tmp_path, "backups") == data_home / "litehive" / wid / "backups"
    assert workspace_path(tmp_path, "logs") == data_home / "litehive" / wid / "logs"
    assert workspace_path(tmp_path, "worktrees") == data_home / "litehive" / wid / "worktrees"
    assert workspace_path(tmp_path, "subagents", "T-0001", "agent-1") == (
        data_home / "litehive" / wid / "subagents" / "T-0001" / "agent-1"
    )
    assert workspace_path(tmp_path, "data.db").exists()
    assert litehive_root() == data_home / "litehive"
    assert litehive_root() / "config.yaml" == data_home / "litehive" / "config.yaml"
    assert _registered_paths(_registry_path(config_home)) == [str(tmp_path.resolve())]

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
        "task_audit_log",
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
    registry_paths = _registered_paths(_registry_path(config_home))
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
    assert set(_registered_paths(_registry_path(config_home))) == {
        str(workspace_one.resolve()),
        str(workspace_two.resolve()),
    }


def test_workspace_registry_uses_only_canonical_config_home_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.registry import list_registered_workspace_paths

    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    workspace_three = tmp_path / "workspace-three"
    canonical_path = _registry_path(config_home)
    stale_data_home_path = data_home / "litehive" / "workspaces.yaml"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    stale_data_home_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text(
        yaml.safe_dump(
            [str(workspace_one)],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    stale_data_home_path.write_text(
        yaml.safe_dump([str(workspace_two)], sort_keys=False),
        encoding="utf-8",
    )

    assert list_registered_workspace_paths() == [workspace_one.resolve()]
    ensure_workspace(workspace_three)

    assert set(_registered_paths(canonical_path)) == {
        str(workspace_one.resolve()),
        str(workspace_three.resolve()),
    }
    assert _registered_paths(stale_data_home_path) == [str(workspace_two.resolve())]


def test_workspace_registry_rebuilds_after_removed_dict_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    registry_path = _registry_path(config_home)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump([{"repo_path": str(tmp_path)}], sort_keys=False),
        encoding="utf-8",
    )

    ensure_workspace(tmp_path)

    backups = sorted(registry_path.parent.glob("workspaces.yaml.corrupt-*"))
    assert backups
    assert _registered_paths(registry_path) == [str(tmp_path.resolve())]


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

def test_legacy_global_state_in_config_home_is_not_migrated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    legacy_root = config_home / "litehive"
    canonical_root = data_home / "litehive"
    legacy_root.mkdir(parents=True, exist_ok=True)

    (legacy_root / "config.yaml").write_text("default_engine: gemini\n", encoding="utf-8")
    (legacy_root / "workspaces.yaml").write_text("- /tmp/legacy-workspace\n", encoding="utf-8")
    (legacy_root / "daemons.yaml").write_text("- workspace: /tmp/legacy-workspace\n", encoding="utf-8")

    ensure_workspace(tmp_path)
    assert capsys.readouterr().err == ""
    assert (legacy_root / "config.yaml").read_text(encoding="utf-8") == "default_engine: gemini\n"
    assert (legacy_root / "daemons.yaml").read_text(encoding="utf-8") == "- workspace: /tmp/legacy-workspace\n"
    assert not (canonical_root / "config.yaml").exists()
    assert not (canonical_root / "daemons.yaml").exists()
    assert _registered_paths(legacy_root / "workspaces.yaml")[0] == str(tmp_path.resolve())


def test_ensure_workspace_skips_task_yaml_rescan_when_runtime_state_is_current(
    tmp_path: Path,
) -> None:
    from litehive.db.schema import connect_workspace_db

    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Current runtime state")

    with connect_workspace_db(tmp_path) as connection:
        connection.execute("DELETE FROM task_state WHERE task_id = ?", (task.id,))
        connection.commit()
        assert connection.execute("SELECT task_id FROM task_state WHERE task_id = ?", (task.id,)).fetchone() is None

    ensure_workspace(tmp_path)

    with connect_workspace_db(tmp_path) as connection:
        assert connection.execute("SELECT task_id FROM task_state WHERE task_id = ?", (task.id,)).fetchone() is None


def test_ensure_workspace_skips_disk_scan_for_bootstrapped_empty_workspace(
    tmp_path: Path,
) -> None:
    from litehive.db.schema import connect_workspace_db

    ensure_workspace(tmp_path)
    ensure_workspace(tmp_path)

    with connect_workspace_db(tmp_path) as connection:
        task_rows = connection.execute("SELECT COUNT(*) FROM task_state").fetchone()[0]
        queue_row = connection.execute("SELECT payload FROM queue WHERE workspace_key = ?", ("workspace",)).fetchone()

    assert task_rows == 0
    assert queue_row is not None
    assert queue_row[0] == "[]"


def test_litehive_home_overrides_default_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom_home = tmp_path / "custom-home"
    monkeypatch.setenv("LITEHIVE_HOME", str(custom_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-state"))

    from litehive.config.paths import litehive_root, workspace_data_dir, workspace_path

    ensure_workspace(tmp_path)

    wid = workspace_data_dir(tmp_path).name
    assert litehive_root() == custom_home
    assert litehive_root() / "config.yaml" == custom_home / "config.yaml"
    assert workspace_path(tmp_path, "data.db") == custom_home / wid / "data.db"
    assert _registered_paths(custom_home / "workspaces.yaml") == [str(tmp_path.resolve())]


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


def test_resolve_process_profile_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="unknown process profile 'unknown_profile'"):
        resolve_process_profile("unknown_profile")
