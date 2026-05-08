import json
from pathlib import Path

import pytest
import yaml

from litehive.config.loading import load_config_for_workspace
from litehive.config.model import (
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    SandboxCredentialInput,
)
from litehive.config.profiles.defaults import PROCESS_PROFILE_OVERLAYS, SHARED_PROCESS_PROFILE
from litehive.config.profiles.loader import resolve_process_profile
from litehive.config.workspace import create_workspace
from litehive.state.persist import load_state
from litehive.workspace import Workspace


def _load_config(root: Path) -> LitehiveConfig:
    return load_config_for_workspace(Workspace.from_path(root))


def test_create_workspace_creates_layout(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    assert (tmp_path / ".litehive" / "config.yaml").exists()
    assert (tmp_path / ".litehive" / ".gitignore").exists()
    assert (tmp_path / ".litehive" / "tasks").exists()
    assert not (tmp_path / ".litehive" / "attention").exists()
    assert "engine-monitoring" not in (tmp_path / ".litehive" / ".gitignore").read_text(encoding="utf-8")


def test_create_workspace_bootstraps_rich_commented_config_once(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    config_path = tmp_path / ".litehive" / "config.yaml"
    contents = config_path.read_text(encoding="utf-8")

    for snippet in [
        "# Static defaults can be edited by hand. Runtime engine routing values below",
        "# Bootstrap default engine; use `litehive engine default <engine>` after init.",
        '# `pool_max_tasks: null` means "no cap"; set an integer to stop after N tasks.',
        "# `pool_stop_on_attention` is retained for config compatibility.",
        "#   credential_inputs[{env_var, mount_path}], extra_ro_binds,",
        "#   extra_rw_binds, setenv",
        "# `auto_commit: false` leaves commit creation to the operator/agent.",
    ]:
        assert snippet in contents

    original = "default_engine: gemini\n"
    config_path.write_text(original, encoding="utf-8")

    create_workspace(tmp_path)

    assert config_path.read_text(encoding="utf-8") == original


def test_create_workspace_bootstraps_runtime_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import litehive_root, workspace_data_dir, workspace_path
    from litehive.db.schema import connect_workspace_db

    create_workspace(tmp_path)

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

    with connect_workspace_db(tmp_path) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert {
        "schema_migrations",
        "pool_state",
        "queue",
        "task_intent",
        "task_state",
        "task_journal",
        "task_audit_log",
        "stage_reports",
        "recovery_reports",
        "hook_artifacts",
        "subagent_sessions",
        "subagent_id_counters",
        "events",
        "engine_monitoring",
        "worktrees",
        "pipeline_transitions",
        "pipeline_journal",
        "pipeline_task_state",
        "pipeline_sessions",
    } <= tables


def test_load_state_requires_existing_workspace_without_creating_it(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an existing Litehive project"):
        load_state(tmp_path)

    assert not (tmp_path / ".litehive").exists()


def test_deprecated_global_state_in_config_home_is_ignored(
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
    (legacy_root / "workspaces.yaml").write_text(
        yaml.safe_dump(
            [
                "/tmp/legacy-workspace",
                str(tmp_path),
                str(tmp_path / "."),
                "/tmp/legacy-workspace",
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (legacy_root / "daemons.yaml").write_text("- workspace: /tmp/legacy-workspace\n", encoding="utf-8")

    create_workspace(tmp_path)
    err = capsys.readouterr().err

    assert err == ""
    assert (legacy_root / "config.yaml").read_text(encoding="utf-8") == "default_engine: gemini\n"
    assert (legacy_root / "workspaces.yaml").exists()
    assert (legacy_root / "daemons.yaml").read_text(encoding="utf-8") == "- workspace: /tmp/legacy-workspace\n"
    assert not (canonical_root / "config.yaml").exists()
    assert not (canonical_root / "daemons.yaml").exists()
    assert not (canonical_root / "workspaces.db").exists()
    (tmp_path / ".litehive" / "config.yaml").write_text("{}", encoding="utf-8")
    assert _load_config(tmp_path).default_engine != "gemini"

    create_workspace(tmp_path)
    assert capsys.readouterr().err == ""


def test_workspace_load_config_is_workspace_bound_entrypoint(tmp_path: Path) -> None:
    from litehive.workspace import Workspace

    create_workspace(tmp_path, LitehiveConfig(default_engine="gemini"))
    workspace = Workspace.from_path(tmp_path)

    config = workspace.load_config()

    assert config.default_engine == "gemini"
    assert workspace.config() is config


def test_create_workspace_skips_task_yaml_rescan_when_runtime_state_is_current(
    tmp_path: Path,
) -> None:
    from litehive.db.schema import connect_workspace_db
    from litehive.state.records import create_task_for_workspace

    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Current runtime state")

    with connect_workspace_db(tmp_path) as connection:
        connection.execute("DELETE FROM task_state WHERE task_id = ?", (task.id,))
        connection.commit()
        assert connection.execute("SELECT task_id FROM task_state WHERE task_id = ?", (task.id,)).fetchone() is None

    create_workspace(tmp_path)

    with connect_workspace_db(tmp_path) as connection:
        assert connection.execute("SELECT task_id FROM task_state WHERE task_id = ?", (task.id,)).fetchone() is None


def test_create_workspace_rebuilds_fresh_database_from_task_event_log(tmp_path: Path) -> None:
    from litehive.config.paths import workspace_path
    from litehive.db.schema import connect_workspace_db
    from litehive.state.records import create_task_for_workspace
    from litehive.tasks.event_log import task_event_log_path

    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Recovered from event log")
    assert task_event_log_path(workspace).exists()

    db_path = workspace_path(tmp_path, "data.db")
    for path in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        path.unlink(missing_ok=True)

    create_workspace(tmp_path)

    with connect_workspace_db(tmp_path) as connection:
        task_row = connection.execute("SELECT task_id FROM task_state WHERE task_id = ?", (task.id,)).fetchone()
        queue_row = connection.execute("SELECT payload FROM queue WHERE workspace_key = ?", ("workspace",)).fetchone()

    assert queue_row is not None
    assert task_row is not None
    assert json.loads(queue_row[0]) == [task.id]


def test_create_workspace_skips_disk_scan_for_bootstrapped_empty_workspace(
    tmp_path: Path,
) -> None:
    from litehive.db.schema import connect_workspace_db

    create_workspace(tmp_path)
    create_workspace(tmp_path)

    with connect_workspace_db(tmp_path) as connection:
        task_rows = connection.execute("SELECT COUNT(*) FROM task_state").fetchone()[0]
        queue_row = connection.execute("SELECT payload FROM queue WHERE workspace_key = ?", ("workspace",)).fetchone()

    assert task_rows == 0
    assert queue_row is not None
    assert queue_row[0] == "[]"


def test_litehive_home_overrides_default_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom_home = tmp_path / "custom-home"
    config_home = tmp_path / "config-home"
    legacy_root = config_home / "litehive"
    legacy_root.mkdir(parents=True, exist_ok=True)
    (legacy_root / "config.yaml").write_text("default_engine: gemini\n", encoding="utf-8")
    (legacy_root / "workspaces.yaml").write_text(
        yaml.safe_dump(["/tmp/legacy-workspace"], sort_keys=False),
        encoding="utf-8",
    )
    (legacy_root / "daemons.yaml").write_text("- workspace: /tmp/legacy-workspace\n", encoding="utf-8")

    monkeypatch.setenv("LITEHIVE_HOME", str(custom_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-state"))

    from litehive.config.paths import litehive_root, workspace_data_dir, workspace_path

    create_workspace(tmp_path)

    wid = workspace_data_dir(tmp_path).name
    assert litehive_root() == custom_home
    assert litehive_root() / "config.yaml" == custom_home / "config.yaml"
    assert not (custom_home / "daemons.yaml").exists()
    assert workspace_path(tmp_path, "data.db") == custom_home / wid / "data.db"
    assert not (custom_home / "workspaces.db").exists()
    assert (legacy_root / "config.yaml").exists()
    assert (legacy_root / "workspaces.yaml").exists()
    assert (legacy_root / "daemons.yaml").exists()


def test_load_config_round_trips_external_engine_sandbox(tmp_path: Path) -> None:
    create_workspace(
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

    config = _load_config(tmp_path)

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


@pytest.mark.parametrize(
    ("sandbox_patch", "message"),
    [
        ({"runtime_args": "--pull=never"}, "external_engine_sandbox.runtime_args"),
        ({"engine_policies": ["codex"]}, "external_engine_sandbox.engine_policies"),
    ],
)
def test_load_config_rejects_malformed_external_engine_sandbox_shapes(
    tmp_path: Path,
    sandbox_patch: dict[str, object],
    message: str,
) -> None:
    create_workspace(tmp_path)
    current_config_path = tmp_path / ".litehive" / "config.yaml"
    raw_config = yaml.safe_load(current_config_path.read_text(encoding="utf-8"))
    raw_config["external_engine_sandbox"] = sandbox_patch
    current_config_path.write_text(yaml.safe_dump(raw_config, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _load_config(tmp_path)


def test_resolve_process_profile_merges_shared_process_with_overlay() -> None:
    profile = resolve_process_profile("codehive")

    assert profile.label == "Codehive-style"
    assert profile.shared_stages == [
        "grooming",
        "implementing",
        "testing",
        "accepting",
        "commit_to_git",
    ]
    assert profile.orchestrator_model
    assert profile.routing_model
    assert profile.role_model
    assert profile.prompt_scaffold
    assert profile.stage_overlay["accepting"]


def test_process_profiles_are_loaded_from_typed_defaults() -> None:
    assert set(PROCESS_PROFILE_OVERLAYS) == {"codehive", "cpp", "django", "generic", "python", "rust"}
    assert PROCESS_PROFILE_OVERLAYS["generic"] == {}
    assert SHARED_PROCESS_PROFILE["label"] == "Generic"

    profile = resolve_process_profile("python")

    assert profile.label == "Python"
    assert profile.prompt_scaffold[0] == SHARED_PROCESS_PROFILE["prompt_scaffold"][0]
    assert profile.workspace_overlay[-1] == "- Keep dependency and packaging changes explicit and minimal."


def test_resolve_process_profile_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="unknown process profile 'unknown_profile'"):
        resolve_process_profile("unknown_profile")
