from tests.workspace_helpers import (
    AdapterCapabilities,
    CLIExecutionResult,
    EngineUsageObservation,
    EngineUsageWindow,
    ExternalCLIAdapter,
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    Path,
    SandboxCredentialInput,
    available_process_profiles,
    create_task,
    ensure_workspace,
    get_task,
    get_engine,
    load_config,
    load_engine_monitoring,
    load_state,
    record_engine_execution,
    render_context_template,
    resolve_process_profile,
    save_task_runtime,
    save_task,
    state_path,
)
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
from pathlib import Path as SysPath
import sqlite3
import threading

import pytest
import yaml

from litehive.state.records import LegacyTaskStateError, TaskStateMissingError


def _register_workspace_in_subprocess(args: tuple[str, str, str, str]) -> str:
    workspace_root, _config_home, data_home, state_home = args
    os.environ["XDG_DATA_HOME"] = data_home
    os.environ["XDG_STATE_HOME"] = state_home
    from litehive.config.workspace import ensure_workspace

    ensure_workspace(SysPath(workspace_root))
    return workspace_root


def test_ensure_workspace_creates_layout(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    assert (tmp_path / ".litehive" / "config.yaml").exists()
    assert not (tmp_path / ".litehive" / "state.yaml").exists()
    assert (tmp_path / ".litehive" / ".gitignore").exists()
    assert (tmp_path / ".litehive" / "tasks").exists()


def test_ensure_workspace_bootstraps_runtime_db_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import (
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

    with sqlite3.connect(litehive_database_path()) as connection:
        rows = connection.execute(
            "SELECT workspace_id, path FROM workspaces ORDER BY path"
        ).fetchall()
    assert rows == [(wid, str(tmp_path.resolve()))]

    with connect_workspace_db(tmp_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
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
    } <= tables


def test_workspace_registry_ignores_legacy_yaml_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import litehive_database_path

    legacy_workspace = tmp_path.parent / f"{tmp_path.name}-legacy-only"
    legacy_workspace.mkdir()
    legacy_path = config_home / "litehive" / "workspaces.yaml"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        yaml.safe_dump([str(legacy_workspace.resolve())], sort_keys=False),
        encoding="utf-8",
    )

    ensure_workspace(tmp_path)

    assert legacy_path.exists()
    with sqlite3.connect(litehive_database_path()) as connection:
        paths = [
            row[0]
            for row in connection.execute("SELECT path FROM workspaces ORDER BY path").fetchall()
        ]
    assert paths == [str(tmp_path.resolve())]


def test_workspace_registry_handles_parallel_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import litehive_database_path, workspace_id

    workspaces = []
    for index in range(8):
        root = tmp_path / f"workspace-{index}"
        root.mkdir()
        workspaces.append(root)

    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as executor:
        results = list(
            executor.map(
                _register_workspace_in_subprocess,
                [
                    (str(root), "", str(data_home), str(state_home))
                    for root in workspaces
                ],
            )
        )

    assert {SysPath(path) for path in results} == set(workspaces)
    with sqlite3.connect(litehive_database_path()) as connection:
        rows = connection.execute(
            "SELECT workspace_id, path FROM workspaces ORDER BY path"
        ).fetchall()
    assert rows == [
        (workspace_id(root), str(root.resolve()))
        for root in sorted(workspaces)
    ]


def test_workspace_registry_rebuilds_after_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import litehive_database_path

    litehive_database_path().parent.mkdir(parents=True, exist_ok=True)
    litehive_database_path().write_text("not a sqlite database", encoding="utf-8")

    ensure_workspace(tmp_path)

    with sqlite3.connect(litehive_database_path()) as connection:
        paths = [
            row[0]
            for row in connection.execute("SELECT path FROM workspaces ORDER BY path").fetchall()
        ]
    assert paths == [str(tmp_path.resolve())]


def test_workspace_registry_uses_thread_local_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_litehive_home_overrides_default_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "custom-home"
    monkeypatch.setenv("LITEHIVE_HOME", str(custom_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-state"))

    from litehive.config.paths import (
        litehive_database_path,
        litehive_root,
        workspace_database_path,
        workspace_id,
    )

    ensure_workspace(tmp_path)

    wid = workspace_id(tmp_path)
    assert litehive_root() == custom_home
    assert litehive_database_path() == custom_home / "litehive.db"
    assert workspace_database_path(tmp_path) == custom_home / wid / "data.db"


def test_ensure_workspace_ignores_legacy_global_and_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    from litehive.config.paths import (
        daemon_registry_path,
        global_config_path,
        workspace_id,
        workspace_logs_dir,
        workspace_worktrees_dir,
    )
    from litehive.daemon.logs import latest_run_all_log_dir

    wid = workspace_id(tmp_path)
    legacy_root = config_home / "litehive"
    legacy_root.mkdir(parents=True, exist_ok=True)
    (legacy_root / "config.yaml").write_text("default_engine: gemini\n", encoding="utf-8")
    (legacy_root / "daemons.yaml").write_text("daemons: {}\n", encoding="utf-8")
    legacy_log = state_home / "litehive" / wid / "logs" / "run-all" / "20260412T010203Z" / "0001-run.log"
    legacy_log.parent.mkdir(parents=True, exist_ok=True)
    legacy_log.write_text("legacy daemon log\n", encoding="utf-8")
    legacy_worktree = tmp_path / ".litehive" / "worktrees" / "T-0001-demo" / "README.md"
    legacy_worktree.parent.mkdir(parents=True, exist_ok=True)
    legacy_worktree.write_text("legacy worktree\n", encoding="utf-8")

    ensure_workspace(tmp_path)
    stderr = capsys.readouterr().err

    assert global_config_path() == data_home / "litehive" / "config.yaml"
    assert daemon_registry_path() == data_home / "litehive" / "daemons.yaml"
    assert not global_config_path().exists()
    assert not daemon_registry_path().exists()
    assert latest_run_all_log_dir(tmp_path) is None
    assert not (
        workspace_logs_dir(tmp_path) / "run-all" / "20260412T010203Z" / "0001-run.log"
    ).exists()
    assert not (workspace_worktrees_dir(tmp_path) / "T-0001-demo" / "README.md").exists()
    assert legacy_log.exists()
    assert legacy_worktree.exists()
    assert stderr == ""


def test_get_task_reads_runtime_from_database_without_runtime_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="DB runtime")
    task.runtime.execution_status = "running"
    task.runtime.current_stage.step = "implementing"
    save_task_runtime(tmp_path, task)

    runtime_path = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "runtime.yaml"
    if runtime_path.exists():
        runtime_path.unlink()

    loaded = get_task(tmp_path, task.id)

    assert loaded is not None
    assert loaded.runtime.execution_status == "running"
    assert loaded.runtime.current_stage.step == "implementing"


def test_task_yaml_persists_only_intent_fields_and_runtime_moves_to_db(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Intent only", auto_commit=False)
    task.model = "gpt-5.4"
    task.status = "flagged"
    task.flag_reason = "needs-review"
    task.flag_count = 2
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage.step = "implementing"
    task.git.commit_sha = "abc123"
    task.git.checkpoint_attempts = 3
    save_task(tmp_path, task)

    task_path = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "task.yaml"
    data = yaml.safe_load(task_path.read_text(encoding="utf-8"))

    assert set(data) == {
        "id",
        "slug",
        "title",
        "created_at",
        "task_type",
        "pipeline_mode",
        "priority",
        "depends_on",
        "goal",
        "acceptance_criteria",
        "constraints",
        "plan",
        "git",
        "created_from",
    }
    assert set(data["git"]) == {"auto_commit", "commit_message"}

    loaded = get_task(tmp_path, task.id)
    assert loaded is not None
    assert loaded.model == "gpt-5.4"
    assert loaded.status == "flagged"
    assert loaded.flag_reason == "needs-review"
    assert loaded.flag_count == 2
    assert loaded.pipeline_status == "implementing"
    assert loaded.git.commit_sha == "abc123"
    assert loaded.git.checkpoint_attempts == 3
    assert loaded.runtime.execution_status == "running"
    assert loaded.runtime.current_stage.step == "implementing"


def test_get_task_rejects_legacy_runtime_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-legacy-runtime"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "legacy-runtime",
                "title": "Legacy runtime",
                "pipeline_mode": "full",
                "priority": "high",
                "git": {
                    "auto_commit": True,
                    "commit_message": "legacy message",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (task_dir / "runtime.yaml").write_text(
        yaml.safe_dump(
            {
                "execution_status": "running",
                "current_stage": {"step": "testing"},
                "git": {"commit_sha": "deadbeef"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(LegacyTaskStateError, match="runtime.yaml"):
        get_task(tmp_path, "T-0001")


def test_get_task_requires_sqlite_runtime_state_row(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task_dir = tmp_path / ".litehive" / "tasks" / "T-0001-missing-runtime"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "T-0001",
                "slug": "missing-runtime",
                "title": "Missing runtime row",
                "pipeline_mode": "full",
                "priority": "medium",
                "git": {
                    "auto_commit": True,
                    "commit_message": "missing runtime row",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(TaskStateMissingError, match="missing its SQLite runtime state row"):
        get_task(tmp_path, "T-0001")


def test_ensure_workspace_scaffolds_workspace_gitignore(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    gitignore = (tmp_path / ".litehive" / ".gitignore").read_text(encoding="utf-8")

    assert "engine-monitoring.yaml" in gitignore
    assert "tasks/*/reports/commit_to_git-*.yaml" in gitignore
    assert "state.yaml" not in gitignore


def test_record_engine_execution_tracks_local_usage_fallback(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="codex",
        adapter=get_engine("codex"),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
        ),
        failure_kind="execution_limit",
        failure_reason="usage limit reached",
    )

    monitoring = load_engine_monitoring(tmp_path)
    record = monitoring.engines["codex"]

    assert record.source == "local"
    assert record.invocation_count == 1
    assert record.success_count == 0
    assert record.failure_count == 1
    assert record.limit_event_count == 1
    assert record.last_limit_kind == "quota"
    assert record.last_limit_reason == "usage limit reached"
    assert record.last_task_id == "T-0001"
    assert record.usage is not None
    assert record.usage.used == 1
    assert record.usage.unit == "requests"


def test_record_engine_execution_accepts_provider_usage_observation(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    class ProviderAdapter(ExternalCLIAdapter):
        def build_command(
            self, prompt: str, cwd: Path, model: str | None = None, *, max_turns: int | None = None
        ) -> list[str]:  # type: ignore[override]
            return ["provider-cli", prompt]

        def extract_usage_observation(
            self, execution: CLIExecutionResult
        ) -> EngineUsageObservation | None:
            return EngineUsageObservation(
                source="provider",
                provider="gemini",
                success=True,
                usage=EngineUsageWindow(used=10, limit=100, remaining=90, unit="requests"),
                metadata={"project": "demo"},
            )

    record_engine_execution(
        tmp_path,
        task_id="T-0002",
        engine_name="gemini",
        adapter=ProviderAdapter(
            name="gemini",
            binary="provider-cli",
            capabilities=AdapterCapabilities(
                supports_model_override=True, transcript_format="jsonl"
            ),
        ),
        execution=CLIExecutionResult(
            adapter="gemini",
            argv=("provider-cli", "run"),
            cwd=tmp_path,
            exit_code=0,
            stdout="{}",
            stderr="",
        ),
        failure_kind=None,
        failure_reason=None,
    )

    monitoring = load_engine_monitoring(tmp_path)
    record = monitoring.engines["gemini"]

    assert record.source == "provider"
    assert record.provider == "gemini"
    assert record.invocation_count == 1
    assert record.success_count == 1
    assert record.failure_count == 0
    assert record.usage is not None
    assert record.usage.remaining == 90
    assert record.metadata["project"] == "demo"


def test_record_engine_execution_tracks_codex_provider_limit_observation(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="codex",
        adapter=get_engine("codex"),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec", "--json"),
            cwd=tmp_path,
            exit_code=1,
            stdout="\n".join(
                [
                    '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"message\\":\\"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 5:26 PM.\\"}}"}',
                    '{"type":"turn.failed","error":{"message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"message\\":\\"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 5:26 PM.\\"}}"}}',
                ]
            ),
            stderr="",
        ),
        failure_kind="execution_limit",
        failure_reason="usage limit reached",
    )

    monitoring = load_engine_monitoring(tmp_path)
    record = monitoring.engines["codex"]

    assert record.source == "provider"
    assert record.provider == "openai"
    assert record.invocation_count == 1
    assert record.success_count == 0
    assert record.failure_count == 1
    assert record.limit_event_count == 1
    assert record.last_limit_kind == "quota"
    assert record.last_limit_reason == "usage limit reached"
    assert record.metadata["error_status"] == 429
    assert record.metadata["error_type"] == "rate_limit_error"
    assert record.metadata["retry_at_hint"] == "5:26 PM"
    assert record.metadata["purchase_more_credits"] is True


def test_record_engine_execution_tracks_claude_provider_limit_observation(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="claude",
        adapter=get_engine("claude"),
        execution=CLIExecutionResult(
            adapter="claude",
            argv=("claude", "-p"),
            cwd=tmp_path,
            exit_code=1,
            stdout=(
                '{"type":"error","error":{"type":"rate_limit_error","message":"Your account has hit a rate limit. '
                'Please retry after a short delay."}}\n'
            ),
            stderr="",
        ),
        failure_kind="execution_limit",
        failure_reason="rate limit reached",
    )

    monitoring = load_engine_monitoring(tmp_path)
    record = monitoring.engines["claude"]

    assert record.source == "provider"
    assert record.provider == "anthropic"
    assert record.invocation_count == 1
    assert record.success_count == 0
    assert record.failure_count == 1
    assert record.limit_event_count == 1
    assert record.last_limit_kind == "rate"
    assert record.last_limit_reason == "rate limit reached"
    assert record.metadata["error_type"] == "rate_limit_error"
    assert record.metadata["error_message"] == (
        "Your account has hit a rate limit. Please retry after a short delay."
    )


def test_record_engine_execution_tracks_opencode_provider_usage_observation(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="opencode",
        adapter=get_engine("opencode"),
        execution=CLIExecutionResult(
            adapter="opencode",
            argv=("opencode", "run", "--format", "json"),
            cwd=tmp_path,
            exit_code=0,
            stdout=(
                '{"type":"text","timestamp":2,"sessionID":"ses_123","part":{"id":"prt_2","type":"text","text":"OK"}}\n'
                '{"type":"step_finish","timestamp":3,"sessionID":"ses_123","part":{"id":"prt_3","type":"step-finish","reason":"stop","cost":0,'
                '"tokens":{"total":10971,"input":10509,"output":14,"reasoning":11,"cache":{"read":448,"write":0}}}}\n'
            ),
            stderr="",
        ),
        failure_kind=None,
        failure_reason=None,
    )

    monitoring = load_engine_monitoring(tmp_path)
    record = monitoring.engines["opencode"]

    assert record.source == "provider"
    assert record.provider == "z.ai"
    assert record.invocation_count == 1
    assert record.success_count == 1
    assert record.failure_count == 0
    assert record.usage is not None
    assert record.usage.used == 10971
    assert record.usage.unit == "tokens"
    assert record.metadata["input_tokens"] == 10509
    assert record.metadata["finish_reason"] == "stop"


def test_ensure_workspace_scaffolds_profile_specific_context(tmp_path: Path) -> None:
    django_path = tmp_path / "django"
    django_path.mkdir()

    from litehive.config.model import LitehiveConfig

    ensure_workspace(django_path, LitehiveConfig(process_profile="django"))

    context = (django_path / ".litehive" / "context.md").read_text(encoding="utf-8")

    assert "# Litehive Workspace Context" in context
    assert "## Django specifics" in context
    assert "migrations" in context
    assert "## Development rules" in context


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

def test_available_process_profiles_include_generic_and_project_templates() -> None:
    assert available_process_profiles() == [
        "codehive",
        "cpp",
        "django",
        "generic",
        "python",
        "rust",
    ]


def test_process_profiles_loader_is_small_and_file_backed() -> None:
    import litehive.config.profiles as profiles_module

    profile_dir = Path(profiles_module.__file__).resolve().parent

    assert len(Path(profiles_module.__file__).read_text(encoding="utf-8").splitlines()) < 50
    assert sorted(path.stem for path in profile_dir.glob("*.yaml")) == [
        "_shared",
        "codehive",
        "cpp",
        "django",
        "generic",
        "python",
        "rust",
    ]


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
    assert (
        profile["orchestrator_model"]
        == "the orchestrator is the manager; subagents execute but do not choose routing."
    )
    assert profile["routing_model"].startswith("manager-owned deterministic routing")
    assert profile["role_model"].startswith("`planner` owns task shaping")
    assert any("generic base prompt" in line for line in profile["prompt_scaffold"])
    assert profile["stage_overlay"]["accepting"][0].startswith(
        "- Reviewer acceptance is managerial"
    )


def test_render_context_template_shows_base_and_project_stage_scaffolding() -> None:
    context = render_context_template("rust")

    assert "# Litehive Workspace Context" in context
    assert "## Development rules" in context
    assert "## Rust specifics" in context
    assert "## Tool usage" in context
    assert "Favor small, compile-safe changes with clear module ownership." in context
    assert "`cargo test`" in context


def test_resolve_process_profile_includes_python_and_django_testing_overlays() -> None:
    python_profile = resolve_process_profile("python")
    django_profile = resolve_process_profile("django")

    assert "- Use `pytest` for automated verification." in python_profile["stage_overlay"]["implementing"]
    assert "- Use `tmp_path` or pytest fixtures instead of manual tempfiles in repo code or `/tmp` setup." in python_profile["stage_overlay"]["implementing"]
    assert "- Mock external calls and integration edges, not the internal logic under test." in python_profile["stage_overlay"]["implementing"]
    assert "- Do not use `time.sleep` in tests; use deterministic synchronization or time control." in python_profile["stage_overlay"]["implementing"]
    assert "- Reject tests that use manual tempfiles where `tmp_path` would make isolation explicit." in python_profile["stage_overlay"]["testing"]
    assert "- Reject tests that mock the unit's internal logic instead of external boundaries." in python_profile["stage_overlay"]["testing"]
    assert "- Reject tests that rely on `time.sleep` instead of deterministic control." in python_profile["stage_overlay"]["testing"]

    assert "- Do not add tests that only prove ORM round-trips, model field persistence, or `CASCADE` behavior." in django_profile["stage_overlay"]["implementing"]
    assert "- Do not test URL resolution separately when request-level coverage already exercises the route." in django_profile["stage_overlay"]["implementing"]
    assert "- Use `setUpTestData` for read-only fixtures that can be shared across tests." in django_profile["stage_overlay"]["implementing"]
    assert "- Prefer `assertContains` for response body checks instead of brittle string matching." in django_profile["stage_overlay"]["implementing"]
    assert "- Reject standalone URL resolution tests unless routing behavior itself is the feature under test." in django_profile["stage_overlay"]["testing"]
    assert "- Reject read-only fixture setup that should use `setUpTestData`." in django_profile["stage_overlay"]["testing"]
    assert "- Reject response assertions that use raw string matching where `assertContains` is the correct Django assertion." in django_profile["stage_overlay"]["testing"]
