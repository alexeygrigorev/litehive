from tests.workspace_helpers import (
    LitehiveConfig,
    Path,
    RuntimeInterruptionState,
    RuntimeStageState,
    RuntimeSubagentState,
    SubagentRef,
    WorkspaceConflictError,
    _block_runner_lock,
    _cmd_run,
    _completed_subagent_result,
    _init_git_repo,
    argparse,
    build_parser,
    create_task,
    drain_task_pool,
    ensure_workspace,
    global_config_path,
    load_config,
    load_state,
    os,
    pytest,
    require_task,
    resolve_engine_name,
    resolve_engine_plan,
    resolve_execution_retry_policy,
    resolve_model,
    run_single_task,
    run_task,
    save_state,
    save_task,
    save_task_runtime,
    task_dir,
    yaml,
)

import litehive.engines.quota.codex_quota as _codex_quota_mod


def test_engine_command_switches_workspace_default_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine="gemini",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.default_engine == "gemini"
    raw_config = yaml.safe_load(
        (tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8")
    )
    assert raw_config["default_engine"] == "gemini"
    assert "default_engine: codex -> gemini" in output


def test_engine_command_parser_accepts_workspace_switch_args() -> None:
    parser = build_parser()

    args = parser.parse_args(["engine", "opencode", "--workspace", "/tmp/demo"])

    assert args.command == "engine"
    assert args.engine_action == "opencode"
    assert args.workspace == Path("/tmp/demo")


def test_engine_status_parser_accepts_optional_engine_name() -> None:
    parser = build_parser()

    args = parser.parse_args(["engine", "status", "codex", "--workspace", "/tmp/demo"])

    assert args.command == "engine"
    assert args.engine_action == "status"
    assert args.engine_name == "codex"
    assert args.workspace == Path("/tmp/demo")


def test_engine_status_command_shows_all_monitored_engines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.models import EngineUsageRecord, EngineUsageWindow, WorkspaceEngineMonitoring
    from litehive.observability._engine_monitoring import save_engine_monitoring
    from litehive.cli import _cmd_engine
    from litehive.engines.quota.claude_quota import ClaudeQuotaStatus
    from litehive.engines.quota.copilot_quota import CopilotQuotaStatus
    from litehive.engines.quota.zai_quota import ZaiQuotaStatus

    monkeypatch.setattr(
        "litehive.cli.engine.check_codex_quota",
        lambda: _codex_quota_mod.CodexQuotaStatus(error="test-disabled"),
    )
    monkeypatch.setattr(
        "litehive.engines.quota.claude_quota.check_claude_quota",
        lambda: ClaudeQuotaStatus(error="no-credentials"),
    )
    monkeypatch.setattr(
        "litehive.engines.quota.copilot_quota.check_copilot_quota",
        lambda: CopilotQuotaStatus(error="gh not on PATH"),
    )
    monkeypatch.setattr(
        "litehive.engines.quota.zai_quota.check_zai_quota",
        lambda: ZaiQuotaStatus(error="goz not on PATH"),
    )

    save_engine_monitoring(
        tmp_path,
        WorkspaceEngineMonitoring(
            engines={
                "codex": EngineUsageRecord(
                    engine="codex",
                    source="provider",
                    provider="openai",
                    observed_at="2026-04-08T22:10:00Z",
                    invocation_count=7,
                    success_count=6,
                    failure_count=1,
                    limit_event_count=1,
                    last_limit_kind="quota",
                    usage=EngineUsageWindow(
                        used=82,
                        limit=100,
                        remaining=18,
                        unit="percent",
                        reset_at="2026-04-09T06:00:00Z",
                    ),
                ),
                "gemini": EngineUsageRecord(
                    engine="gemini",
                    source="local",
                    observed_at="2026-04-08T21:00:00Z",
                    last_invoked_at="2026-04-08T21:00:00Z",
                    invocation_count=3,
                    success_count=2,
                    failure_count=1,
                    limit_event_count=0,
                ),
            }
        ),
    )

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="status",
            engine_name=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "workspace: " in output
    assert "engine: codex" in output
    assert "invocations: 7" in output
    assert "successes: 6" in output
    assert "failures: 1" in output
    assert "limits: 1" in output
    assert "last_used: 2026-04-08T22:10:00Z" in output
    assert "engine: gemini" in output
    assert "invocations: 3" in output
    assert "last_used: 2026-04-08T21:00:00Z" in output
    assert "=== live quota ===" in output
    assert "quota: unavailable (test-disabled)" in output
    assert "quota: unavailable (no-credentials)" in output
    assert "quota: unavailable (gh not on PATH)" in output
    assert "quota: unavailable (goz not on PATH)" in output


def test_engine_status_command_scopes_to_single_engine_and_shows_codex_quota(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine
    from litehive.engines.quota.codex_quota import CodexQuotaStatus, CodexQuotaWindow

    def fake_check_codex_quota():
        return CodexQuotaStatus(
            limit_reached=True,
            primary_window=CodexQuotaWindow(
                used_percent=100.0,
                reset_at="2026-04-09T05:00:00Z",
            ),
            secondary_window=CodexQuotaWindow(
                used_percent=34.0,
                reset_at="2026-04-14T00:00:00Z",
            ),
            checked_at=1.0,
        )

    monkeypatch.setattr("litehive.cli.engine.check_codex_quota", fake_check_codex_quota)

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="status",
            engine_name="codex",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine: codex" in output
    assert "available: no" in output
    assert "usage_used: 100" in output
    assert "usage_limit: 100" in output
    assert "used_percent: 100.0" in output
    assert "limit_reached: yes" in output
    assert "reset_at: 2026-04-14T00:00:00Z" in output


def test_engine_status_command_shows_claude_quota_without_monitoring_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine
    from litehive.engines.quota.claude_quota import ClaudeQuotaStatus, ClaudeQuotaWindow

    def fake_check_claude_quota():
        return ClaudeQuotaStatus(
            limit_reached=False,
            five_hour=ClaudeQuotaWindow(
                used_percent=42.0,
                reset_at="2026-04-09T17:00:00Z",
            ),
            seven_day=ClaudeQuotaWindow(
                used_percent=63.0,
                reset_at="2026-04-15T00:00:00Z",
            ),
            checked_at=1.0,
        )

    monkeypatch.setattr(
        "litehive.engines.quota.claude_quota.check_claude_quota",
        fake_check_claude_quota,
    )

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="status",
            engine_name="claude",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_status: no monitoring data for claude" in output
    assert "engine: claude" in output
    assert "5h_used: 42%" in output
    assert "7d_used: 63%" in output
    assert "5h_resets: 2026-04-09T17:00:00Z" in output
    assert "7d_resets: 2026-04-15T00:00:00Z" in output


def test_engine_status_command_shows_copilot_quota_without_monitoring_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine
    from litehive.engines.quota.copilot_quota import CopilotQuotaStatus

    def fake_check_copilot_quota():
        return CopilotQuotaStatus(
            limit_reached=False,
            premium_remaining=37,
            premium_entitlement=100,
            premium_percent_remaining=37.0,
            quota_reset_date="2026-05-01",
            checked_at=1.0,
        )

    monkeypatch.setattr(
        "litehive.engines.quota.copilot_quota.check_copilot_quota",
        fake_check_copilot_quota,
    )

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="status",
            engine_name="copilot",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_status: no monitoring data for copilot" in output
    assert "engine: copilot" in output
    assert "premium_remaining: 37/100" in output
    assert "percent_remaining: 37%" in output
    assert "resets: 2026-05-01" in output


def test_engine_status_command_shows_zai_quota_without_monitoring_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine
    from litehive.engines.quota.zai_quota import ZaiQuotaStatus, ZaiQuotaWindow

    def fake_check_zai_quota():
        return ZaiQuotaStatus(
            limit_reached=True,
            api_calls=ZaiQuotaWindow(used_percent=81.0, window_hours=24, remaining=19, limit=100),
            tokens=ZaiQuotaWindow(used_percent=64.0, window_hours=24, remaining=360, limit=1000),
            checked_at=1.0,
        )

    monkeypatch.setattr(
        "litehive.engines.quota.zai_quota.check_zai_quota",
        fake_check_zai_quota,
    )

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="status",
            engine_name="opencode",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_status: no monitoring data for opencode" in output
    assert "engine: opencode" in output
    assert "api_calls_used: 81%" in output
    assert "tokens_used: 64%" in output
    assert "limit_reached: yes" in output


def test_engine_status_command_handles_live_quota_errors_gracefully(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine

    def fake_check_copilot_quota():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "litehive.engines.quota.copilot_quota.check_copilot_quota",
        fake_check_copilot_quota,
    )

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="status",
            engine_name="copilot",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "quota: unavailable (boom)" in output


def test_engine_status_command_reports_no_data_for_requested_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine

    exit_code = _cmd_engine(
        argparse.Namespace(
            workspace=tmp_path,
            engine_action="status",
            engine_name="gemini",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_status: no monitoring data for gemini" in output


def test_switch_command_parser_accepts_task_engine_reason_and_workspace() -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["switch", "T-0007", "gemini", "--reason", "quota exhausted", "--workspace", "/tmp/demo"]
    )

    assert args.command == "switch"
    assert args.task_id == "T-0007"
    assert args.engine == "gemini"
    assert args.reason == "quota exhausted"
    assert args.workspace == Path("/tmp/demo")


def test_configure_persists_gemini_model(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="gemini",
        process_profile="generic",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model="gemini-2.5-pro",
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.default_engine == "gemini"
    assert config.gemini_model == "gemini-2.5-pro"


def test_configure_persists_copilot_model(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="copilot",
        process_profile="generic",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model="gpt-5",
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.default_engine == "copilot"
    assert config.copilot_model == "gpt-5"


def test_configure_persists_process_profile(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="rust",
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    context = (tmp_path / ".litehive" / "context.md").read_text(encoding="utf-8")

    assert config.process_profile == "rust"
    assert "# Litehive Workspace Context" in context
    assert "## Rust specifics" in context


def test_configure_persists_pool_stop_defaults(tmp_path: Path) -> None:
    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        pool_stop_on_failure=True,
        pool_max_tasks=2,
        pool_stop_on_limit=True,
        pool_quota_threshold=3,
        pool_budget_threshold=1,
        pool_stop_on_dirty_git=True,
        pool_selection_policy="priority_first",
    )

    from litehive.cli import _cmd_configure

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.pool_stop_on_failure is True
    assert config.pool_max_tasks == 2
    assert config.pool_stop_on_execution_limit is True
    assert config.pool_quota_threshold == 3
    assert config.pool_budget_threshold == 1
    assert config.pool_stop_on_dirty_git is True
    assert config.pool_selection_policy == "priority_first"


def test_load_config_uses_global_defaults_when_workspace_config_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    global_path = global_config_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        yaml.safe_dump(
            {
                "default_engine": "gemini",
                "pool_stop_on_failure": True,
                "engine_costs": {"codex": 9},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)

    config = load_config(workspace)

    assert config.default_engine == "gemini"
    assert config.pool_stop_on_failure is True
    assert config.engine_costs["codex"] == 9
    assert config.engine_costs["claude"] == 3


def test_load_config_applies_workspace_overrides_on_top_of_global_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    global_path = global_config_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        yaml.safe_dump(
            {
                "default_engine": "gemini",
                "engine_costs": {"codex": 9, "claude": 7},
                "subagent_resource_limits": {
                    "enabled": True,
                    "memory_mb": 4096,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    (workspace / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "default_engine": "codex",
                "engine_costs": {"claude": 4},
                "subagent_resource_limits": {"cpu_count": 2.0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(workspace)

    assert config.default_engine == "codex"
    assert config.engine_costs["codex"] == 9
    assert config.engine_costs["claude"] == 4
    assert config.subagent_resource_limits.enabled is True
    assert config.subagent_resource_limits.memory_mb == 4096
    assert config.subagent_resource_limits.cpu_count == 2.0


def test_resolve_engine_name_prefers_run_override_then_task_then_workspace_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Pending task", engine="opencode")

    assert resolve_engine_name(task, config, engine_override="gemini") == "gemini"
    assert resolve_engine_name(task, config) == "opencode"

    task.engine = None
    assert resolve_engine_name(task, config) == config.default_engine


def test_resolve_model_prefers_run_override_then_task_then_workspace_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="opencode",
            opencode_model="zai-coding-plan/glm-5.1",
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Pending task", engine="opencode", model="custom-task-model")

    assert (
        resolve_model(task, config, engine_name="opencode", model_override="run-model")
        == "run-model"
    )
    assert resolve_model(task, config, engine_name="opencode") == "custom-task-model"

    task.model = None
    assert resolve_model(task, config, engine_name="opencode") == "zai-coding-plan/glm-5.1"


def test_resolve_model_skips_unsupported_engine_override(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Pending task", engine="codex", model="custom-task-model")

    assert resolve_model(task, config, engine_name="codex", model_override="run-model") is None


def test_litehive_config_normalizes_execution_retry_policies() -> None:
    config = LitehiveConfig(
        execution_retry_policies={
            "external_cli": {
                "max_retries": 2,
                "backoff_seconds": 0.25,
                "backoff_multiplier": 2.0,
                "retry_on": ["timeout", "network", "timeout"],
            },
            "gemini": {
                "max_retries": 1,
                "backoff_seconds": 1.0,
                "backoff_multiplier": 1.0,
                "retry_on": ["service"],
            },
            "model_family:GLM": {
                "max_retries": 3,
                "backoff_seconds": 0.5,
                "backoff_multiplier": 1.0,
                "retry_on": ["network"],
            },
        }
    )

    assert config.execution_retry_policies["external_cli"].max_retries == 2
    assert config.execution_retry_policies["external_cli"].retry_on == ["timeout", "network"]
    assert resolve_execution_retry_policy(config, engine_name="codex").selector == "external_cli"
    assert resolve_execution_retry_policy(config, engine_name="gemini").selector == "gemini"
    assert config.execution_retry_policies["model_family:glm"].max_retries == 3
    assert (
        resolve_execution_retry_policy(
            config,
            engine_name="opencode",
            model_name="zai-coding-plan/glm-5.1",
        ).selector
        == "model_family:glm"
    )


def test_litehive_config_normalizes_external_cli_engine_category_alias() -> None:
    config = LitehiveConfig(
        execution_retry_policies={
            "engine_category:external_cli": {
                "max_retries": 1,
                "backoff_seconds": 0.25,
                "backoff_multiplier": 2.0,
                "retry_on": ["timeout"],
            }
        }
    )

    assert list(config.execution_retry_policies) == ["external_cli"]


def test_litehive_config_defaults_include_claude_retry_policy() -> None:
    config = LitehiveConfig()

    assert config.subagent_inactivity_timeout_seconds == 360.0
    assert config.execution_retry_policies["claude"].max_retries == 2
    assert config.execution_retry_policies["claude"].backoff_seconds == 0.25
    assert config.execution_retry_policies["claude"].backoff_multiplier == 2.0
    assert config.execution_retry_policies["claude"].retry_on == [
        "timeout",
        "network",
        "service",
    ]
    assert config.execution_retry_policies["codex"].max_retries == 2
    assert config.execution_retry_policies["codex"].backoff_seconds == 0.25
    assert config.execution_retry_policies["codex"].backoff_multiplier == 2.0
    assert config.execution_retry_policies["codex"].retry_on == [
        "timeout",
        "network",
        "service",
    ]
    assert config.execution_retry_policies["opencode"].max_retries == 2
    assert config.execution_retry_policies["opencode"].backoff_seconds == 0.25
    assert config.execution_retry_policies["opencode"].backoff_multiplier == 2.0
    assert config.execution_retry_policies["opencode"].retry_on == [
        "timeout",
        "network",
        "service",
    ]
    assert config.execution_retry_policies["gemini"].max_retries == 2
    assert config.execution_retry_policies["gemini"].backoff_seconds == 0.25
    assert config.execution_retry_policies["gemini"].backoff_multiplier == 2.0
    assert config.execution_retry_policies["gemini"].retry_on == [
        "timeout",
        "network",
        "service",
    ]
    assert (
        resolve_execution_retry_policy(
            config,
            engine_name="opencode",
            model_name="zai-coding-plan/glm-5.1",
        ).selector
        == "opencode"
    )
    assert (
        resolve_execution_retry_policy(
            config,
            engine_name="gemini",
            model_name="gemini-2.5-pro",
        ).selector
        == "gemini"
    )


def test_load_config_reads_subagent_inactivity_timeout_override(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    raw_config = yaml.safe_load(
        (tmp_path / ".litehive" / "config.yaml").read_text(encoding="utf-8")
    )
    raw_config["subagent_inactivity_timeout_seconds"] = 42
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.subagent_inactivity_timeout_seconds == 42.0


def test_resolve_execution_retry_policy_prefers_claude_selector_before_model_family_and_external_cli() -> (
    None
):
    config = LitehiveConfig(
        claude_enabled=True,
        execution_retry_policies={
            "claude": {
                "max_retries": 1,
                "backoff_seconds": 0.25,
                "backoff_multiplier": 2.0,
                "retry_on": ["network"],
            },
            "model_family:claude": {
                "max_retries": 3,
                "backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "retry_on": ["timeout"],
            },
            "external_cli": {
                "max_retries": 5,
                "backoff_seconds": 9.0,
                "backoff_multiplier": 2.0,
                "retry_on": ["service"],
            },
        },
    )

    resolved = resolve_execution_retry_policy(
        config,
        engine_name="claude",
        model_name="claude-sonnet-4-20250514",
    )

    assert resolved.selector == "claude"
    assert resolved.policy.max_retries == 1


def test_resolve_execution_retry_policy_prefers_codex_selector_before_external_cli() -> None:
    config = LitehiveConfig(
        execution_retry_policies={
            "codex": {
                "max_retries": 1,
                "backoff_seconds": 0.1,
                "backoff_multiplier": 1.0,
                "retry_on": ["timeout"],
            },
            "external_cli": {
                "max_retries": 3,
                "backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "retry_on": ["service"],
            },
        }
    )

    resolved = resolve_execution_retry_policy(config, engine_name="codex")

    assert resolved.selector == "codex"
    assert resolved.policy.max_retries == 1


def test_resolve_engine_name_ignores_title_keywords_uses_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "codex"
    assert resolve_engine_plan(task, config) == ["codex"]


def test_resolve_engine_name_ignores_task_type_for_engine_selection(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior", task_type="review")

    assert resolve_engine_name(task, config) == "codex"
    assert resolve_engine_plan(task, config) == ["codex"]


def test_resolve_engine_name_uses_default_engine_without_task_override(
    tmp_path: Path,
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="gemini",
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "gemini"
    assert resolve_engine_plan(task, config) == ["gemini"]


def test_configure_no_longer_has_task_engine_routing(tmp_path: Path) -> None:
    from litehive.cli import _cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_enabled=False,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=30,
        pool_usage_cap=None,
        pool_cost_cap=None,
        engine_usage_cap=None,
        engine_budget_cap=None,
        engine_cost=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.default_engine == "codex"


def test_load_config_rejects_legacy_pre_acceptance_command(tmp_path: Path) -> None:
    from litehive.config import ensure_workspace, config_path

    ensure_workspace(tmp_path)
    import yaml

    cfg = yaml.safe_load(config_path(tmp_path).read_text(encoding="utf-8")) or {}
    cfg["pre_acceptance_command"] = "uv run ruff check litehive tests"
    config_path(tmp_path).write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="pre_acceptance_command is no longer supported"):
        load_config(tmp_path)


def test_configure_persists_runner_hooks(tmp_path: Path) -> None:
    from litehive.cli import _cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_enabled=False,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=30,
        pool_usage_cap=None,
        pool_cost_cap=None,
        engine_usage_cap=None,
        engine_budget_cap=None,
        engine_cost=None,
        task_engine_route=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
        pre_acceptance_command=None,
        hook=[
            "before_swe_implementation=nonblocking:echo pre",
            "after_swe_implementation=blocking:echo post",
            "before_pm_acceptance=blocking:echo review",
            "after_pm_acceptance=nonblocking:echo accepted",
            "after_merge=blocking:echo verify",
        ],
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)

    assert config.runner_hooks["before_swe_implementation"][0].blocking is False
    assert config.runner_hooks["after_swe_implementation"][0].command == "echo post"
    assert config.runner_hooks["before_pm_acceptance"][0].command == "echo review"
    assert config.runner_hooks["after_pm_acceptance"][0].blocking is False
    assert config.runner_hooks["after_merge"][0].command == "echo verify"


def test_load_config_preserves_runner_hook_descriptions(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "before_pm_acceptance": [
                        {
                            "command": "uv run ruff check .",
                            "blocking": True,
                            "description": "ensures lint passes before acceptance",
                        }
                    ]
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runner_hooks["before_pm_acceptance"][0].description == (
        "ensures lint passes before acceptance"
    )


def test_configure_rejects_invalid_runner_hook_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from litehive.cli import _cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_enabled=False,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=30,
        pool_usage_cap=None,
        pool_cost_cap=None,
        engine_usage_cap=None,
        engine_budget_cap=None,
        engine_cost=None,
        task_engine_route=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
        pre_acceptance_command=None,
        hook=["before_testing=blocking:echo nope"],
        subagent_resource_limits_enabled=None,
        subagent_memory_mb=None,
        subagent_cpu_count=None,
        subagent_process_limit=None,
    )

    assert _cmd_configure(parser) == 1
    output = capsys.readouterr().out

    assert "configure failed: runner_hooks key must be one of:" in output


def test_build_parser_accepts_run_dry_run_flag(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["run", "--workspace", str(tmp_path), "--dry-run"])

    assert args.command == "run"
    assert args.workspace == tmp_path
    assert args.dry_run is True
    assert args.drain is False
    assert args.engine is None


def test_build_parser_accepts_run_drain_flag(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["run", "--workspace", str(tmp_path), "--drain"])

    assert args.command == "run"
    assert args.workspace == tmp_path
    assert args.drain is True
    assert args.dry_run is False


def test_build_parser_accepts_model_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        ["add", "Ship task", "--workspace", str(tmp_path), "--model", "gemini-2.5-pro"]
    )
    run_args = parser.parse_args(["run", "--workspace", str(tmp_path), "--model", "gpt-5"])
    update_args = parser.parse_args(
        ["update", "T-0001", "--workspace", str(tmp_path), "--model", "default"]
    )

    assert add_args.model == "gemini-2.5-pro"
    assert run_args.model == "gpt-5"
    assert update_args.model == "default"


def test_build_parser_accepts_acceptance_criteria_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "add",
            "Ship task",
            "--workspace",
            str(tmp_path),
            "--acceptance-criteria",
            "first criterion",
            "--acceptance-criteria",
            "second criterion",
        ]
    )
    update_args = parser.parse_args(
        [
            "update",
            "T-0001",
            "--workspace",
            str(tmp_path),
            "--acceptance-criteria",
            "none",
        ]
    )

    assert add_args.acceptance_criteria == ["first criterion", "second criterion"]
    assert update_args.acceptance_criteria == ["none"]


def test_build_parser_accepts_pm_sizing_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "add",
            "Ship task",
            "--workspace",
            str(tmp_path),
            "--pm-complexity",
            "complex",
            "--planned-effort",
            "l",
        ]
    )
    update_args = parser.parse_args(
        [
            "update",
            "T-0001",
            "--workspace",
            str(tmp_path),
            "--pm-complexity",
            "none",
            "--planned-effort",
            "none",
        ]
    )

    assert add_args.pm_complexity == "complex"
    assert add_args.planned_effort == "l"
    assert update_args.pm_complexity == "none"
    assert update_args.planned_effort == "none"


def test_build_parser_accepts_human_checkpoint_flags(tmp_path: Path) -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "add",
            "Ship task",
            "--workspace",
            str(tmp_path),
            "--human-checkpoint",
            "before_acceptance",
            "--human-checkpoint",
            "before_commit",
        ]
    )
    update_args = parser.parse_args(
        [
            "update",
            "T-0001",
            "--workspace",
            str(tmp_path),
            "--human-checkpoint",
            "none",
        ]
    )

    assert add_args.human_checkpoint == ["before_acceptance", "before_commit"]
    assert update_args.human_checkpoint == ["none"]


def test_build_parser_accepts_web_monitor_flags(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["web", "--workspace", str(tmp_path), "--host", "127.0.0.1", "--port", "9001"]
    )

    assert args.command == "web"
    assert args.workspace == tmp_path
    assert args.host == "127.0.0.1"
    assert args.port == 9001


def test_cmd_run_dry_run_shows_planned_tasks_and_stop_conditions_without_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", engine="opencode")

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_single_task should not be called for dry-run")

    def fail_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run.run_single_task", fail_run_single)
    monkeypatch.setattr("litehive.cli.run.drain_task_pool", fail_drain)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "dry_run: true" in output
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 Pending task" in output
    assert "engine=opencode" in output
    assert "engine_attempts=opencode, codex, gemini, copilot" in output
    assert "model=zai-coding-plan/glm-5.1" in output
    assert "human_checkpoints=-" in output
    assert "predicted_stop_condition: single task complete" in output
    assert "predicted_stop_reason: single_task_complete" in output
    assert "stop_on_failure: False" in output
    assert load_state(tmp_path).queue == ["T-0001"]


def test_cmd_run_dry_run_prefers_run_engine_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", engine="opencode")

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_single_task should not be called for dry-run")

    def fail_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run.run_single_task", fail_run_single)
    monkeypatch.setattr("litehive.cli.run.drain_task_pool", fail_drain)

    exit_code = _cmd_run(
        argparse.Namespace(workspace=tmp_path, dry_run=True, engine="gemini", drain=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 Pending task" in output
    assert "engine=gemini" in output
    assert "engine_attempts=gemini, codex, opencode, copilot" in output
    assert "model=-" in output
    assert "human_checkpoints=-" in output
    assert load_state(tmp_path).queue == ["T-0001"]


def test_cmd_run_dry_run_prefers_run_model_override_without_mutating_workspace_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="opencode", opencode_model="zai-coding-plan/glm-5.1"),
    )
    create_task(
        tmp_path, title="Pending task", engine="opencode", model="task-model", auto_commit=False
    )

    config_before = load_config(tmp_path)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=False,
            engine=None,
            model="run-model",
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine=opencode" in output
    assert "model=run-model" in output
    assert load_config(tmp_path) == config_before


def test_cmd_run_dry_run_plans_dependency_aware_pool_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked dependent", auto_commit=False)
    prerequisite = create_task(tmp_path, title="Prerequisite", engine="opencode", auto_commit=False)

    blocked.depends_on = [prerequisite.id]
    save_task(tmp_path, blocked)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 2" in output
    assert "would_run: 1. T-0002 Prerequisite" in output
    assert "would_run: 2. T-0001 Blocked dependent" in output
    assert "blocked_tasks: 0" in output


def test_cmd_run_drain_dry_run_reports_queue_exhausted_without_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", engine="opencode", auto_commit=False)

    def fail_drain_task_pool(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run.drain_task_pool", fail_drain_task_pool)

    state_before = load_state(tmp_path).model_dump()
    task_before = require_task(tmp_path, "T-0001").model_dump()

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 Pending task" in output
    assert "engine=opencode" in output
    assert "engine_attempts=opencode, codex, gemini, copilot" in output
    assert "predicted_stop_reason: queue_exhausted" in output
    assert load_state(tmp_path).model_dump() == state_before
    assert require_task(tmp_path, "T-0001").model_dump() == task_before
    assert not (tmp_path / ".litehive" / "pool-summary.txt").exists()
    assert not (tmp_path / ".litehive" / "logs" / "pool-runs").exists()


def test_cmd_run_drain_dry_run_reports_empty_queue_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "blocked_tasks: 0" in output
    assert "predicted_stop_reason: queue_exhausted" in output


def test_cmd_run_drain_dry_run_reports_blocked_tasks_remaining_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Blocked task", engine="codex", auto_commit=False)
    task.depends_on = ["T-9999"]
    save_task(tmp_path, task)

    state_before = load_state(tmp_path).model_dump()
    task_before = require_task(tmp_path, task.id).model_dump()

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "blocked_tasks: 1" in output
    assert f"blocked: {task.id} Blocked task blocked_by=T-9999 (missing)" in output
    assert "predicted_stop_reason: blocked_tasks_remaining" in output
    assert load_state(tmp_path).model_dump() == state_before
    assert require_task(tmp_path, task.id).model_dump() == task_before


def test_cmd_run_drain_dry_run_reports_dirty_git_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    _init_git_repo(tmp_path)
    (tmp_path / "app.txt").write_text("dirty\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=True,
            stop_on_dirty_git=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "predicted_stop_reason: dirty_git_state" in output


def test_cmd_run_drain_dry_run_keeps_dirty_git_stop_for_ambiguous_interrupted_ownership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    first = create_task(tmp_path, title="First interrupted task", auto_commit=False)
    second = create_task(tmp_path, title="Second interrupted task", auto_commit=False)

    for task in (first, second):
        task.status = "interrupted"
        task.pipeline_status = "testing"
        task.acceptance_criteria = ["Resume the interrupted testing stage."]
        task.runtime.execution_status = "interrupted"
        task.runtime.current_stage = RuntimeStageState(
            step="testing",
            status="interrupted",
            started_at="2026-04-01T00:00:00+00:00",
            completed_at="2026-04-01T00:01:00+00:00",
            updated_at="2026-04-01T00:01:00+00:00",
            duration_seconds=60,
            verdict="blocked",
            summary="Execution interrupted. Resume from `testing`.",
        )
        task.runtime.interruption = RuntimeInterruptionState(
            source="runner",
            stage="testing",
            pipeline_status="testing",
            resume_stage="testing",
            reason="Interrupted run recovered after stale runner detection.",
            summary="Resume from `testing`.",
            interrupted_at="2026-04-01T00:01:00+00:00",
            detected_at="2026-04-01T00:01:05+00:00",
        )
        save_task(tmp_path, task)
        save_task_runtime(tmp_path, task)
        (task_dir(tmp_path, task) / "reports" / "implementing-001.yaml").write_text(
            yaml.safe_dump(
                {
                    "task_id": task.id,
                    "step": "implementing",
                    "verdict": "pass",
                    "summary": "implemented task changes",
                    "files_changed": ["app.txt"],
                    "tests": {"added": 0, "passing": 0},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    (tmp_path / "app.txt").write_text("dirty\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=True,
            stop_on_dirty_git=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "predicted_stop_reason: dirty_git_state" in output


def test_cmd_run_dry_run_reports_max_tasks_predicted_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(workspace=tmp_path, dry_run=True, max_tasks=1, drain=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2." not in output
    assert "predicted_stop_reason: max_tasks_reached" in output


def test_cmd_run_dry_run_predicts_pool_usage_cap_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(workspace=tmp_path, dry_run=True, pool_usage_cap=1, drain=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 1" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2." not in output
    assert "predicted_stop_reason: pool_usage_cap_reached" in output


def test_cmd_run_dry_run_predicts_pool_cost_cap_stop_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=True,
            pool_cost_cap=3,
            engine_cost=["codex=2"],
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 2" in output
    assert "would_run: 1. T-0001 First task" in output
    assert "would_run: 2. T-0002 Second task" in output
    assert "engine=opencode" in output
    assert "predicted_stop_reason: pool_cost_cap_reached" in output


def test_cmd_run_dry_run_predicts_claude_budget_block_without_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            claude_enabled=True,
            engine_budget_caps={"claude": 2},
            engine_costs={"claude": 3},
            engine_preference=[],
        ),
    )
    create_task(tmp_path, title="Claude task", engine="claude", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=False,
            engine=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "planned_tasks: 0" in output
    assert "predicted_stop_reason: execution_limit_fallbacks_exhausted" in output
    assert "engine_budget_caps: claude=2" in output
    assert "engine_costs: claude=3" in output


def test_cmd_run_dry_run_uses_budget_allowed_fallback_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Research engine quota behavior", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=False,
            engine_usage_cap=["gemini=0"],
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "would_run: 1. T-0001 Research engine quota behavior" in output
    assert "engine=codex" in output
    assert "engine_attempts=codex, opencode, gemini, copilot" in output
    assert "predicted_stop_reason: single_task_complete" in output


def test_drain_task_pool_uses_run_engine_override_for_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", engine="codex", auto_commit=False)
    seen_engines: list[str] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_engines.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path, engine_override="opencode")

    assert summary.stop_reason == "queue_exhausted"
    assert seen_engines == ["opencode", "opencode", "opencode", "opencode"]


def test_run_single_task_uses_run_engine_override_for_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", engine="codex", auto_commit=False)
    seen_engines: list[str] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_engines.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_single_task(tmp_path, engine_override="opencode")

    assert summary.stop_reason == "single_task_complete"
    assert summary.execution is not None
    assert summary.execution.task is not None
    assert summary.execution.task.id == "T-0001"
    assert seen_engines == ["opencode", "opencode", "opencode", "opencode"]
    assert load_state(tmp_path).queue == []


def test_run_single_task_model_precedence_uses_run_override_then_task_then_workspace_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(default_engine="opencode", opencode_model="workspace-model"),
    )
    create_task(
        tmp_path, title="Pending task", engine="opencode", model="task-model", auto_commit=False
    )
    seen_models: list[str | None] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_models.append(model)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    run_single_task(tmp_path, model_override="run-model")
    assert seen_models == ["run-model", "run-model", "run-model", "run-model"]

    seen_models.clear()
    create_task(
        tmp_path, title="Pending task 2", engine="opencode", model="task-model-2", auto_commit=False
    )
    run_single_task(tmp_path)
    assert seen_models == ["task-model-2", "task-model-2", "task-model-2", "task-model-2"]

    seen_models.clear()
    create_task(tmp_path, title="Pending task 3", engine="opencode", auto_commit=False)
    run_single_task(tmp_path)
    assert seen_models == [
        "workspace-model",
        "workspace-model",
        "workspace-model",
        "workspace-model",
    ]


def test_run_single_task_does_not_pass_model_override_to_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(
        tmp_path, title="Pending task", engine="codex", model="task-model", auto_commit=False
    )
    seen_models: list[str | None] = []

    def fake_run(
        self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None
    ):  # type: ignore[no-untyped-def]
        seen_models.append(model)
        return _completed_subagent_result(tmp_path, task.pipeline_status, task=task)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_single_task(tmp_path, model_override="run-model")

    assert summary.stop_reason == "single_task_complete"
    assert seen_models == [None, None, None, None]


def test_cmd_run_dry_run_budget_overrides_do_not_mutate_workspace_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            pool_usage_cap=8,
            pool_cost_cap=20,
            engine_usage_caps={"codex": 4},
            engine_budget_caps={"claude": 9},
            engine_costs={"codex": 1, "claude": 3},
        ),
    )
    create_task(tmp_path, title="Pending task", auto_commit=False)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=True,
            drain=True,
            engine=None,
            pool_usage_cap=1,
            pool_cost_cap=2,
            engine_usage_cap=["codex=0"],
            engine_budget_cap=["claude=2"],
            engine_cost=["codex=5", "claude=7"],
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "predicted_stop_reason: pool_usage_cap_reached" in output
    assert "pool_usage_cap: 1" in output
    assert "pool_cost_cap: 2" in output
    assert "engine_usage_caps: codex=0" in output
    assert "engine_budget_caps: claude=2" in output
    assert "engine_costs: claude=7, codex=5" in output

    config = load_config(tmp_path)
    assert config.pool_usage_cap == 8
    assert config.pool_cost_cap == 20
    assert config.engine_usage_caps == {"codex": 4}
    assert config.engine_budget_caps == {"claude": 9}
    assert config.engine_costs["codex"] == 1
    assert config.engine_costs["claude"] == 3


def test_drain_task_pool_wraps_pool_execution_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert summary.stop_reason == "queue_exhausted"
    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        "T-0001",
        "T-0002",
    ]


def test_run_task_rejects_starting_a_second_active_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    pending = create_task(tmp_path, title="Pending task", auto_commit=False)

    active.runtime.execution_status = "running"
    save_task_runtime(tmp_path, active)
    state = load_state(tmp_path)
    state.active_task_id = active.id
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": os.getpid()}, sort_keys=False),
        encoding="utf-8",
    )
    _block_runner_lock(monkeypatch)

    with pytest.raises(
        WorkspaceConflictError,
        match=f"task {pending.id} cannot start because task {active.id} is already active",
    ):
        run_task(tmp_path, pending)


def test_run_task_recovers_stale_active_task_before_conflict_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(
        tmp_path,
        title="Stale active task",
        acceptance_criteria=["Resume from the same stage after stale process recovery."],
        auto_commit=False,
    )
    pending = create_task(
        tmp_path,
        title="Pending task",
        acceptance_criteria=["Run after stale active state is recovered."],
        auto_commit=False,
    )

    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.runtime.execution_status = "running"
    active.runtime.run_started_at = "2026-04-01T00:00:00+00:00"
    active.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    active.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
        pid=999999,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:00:10+00:00",
    )
    active.subagents.append(
        SubagentRef(
            id="SA-0001",
            role="qa",
            engine="codex",
            status="running",
            path="subagents/SA-0001-qa",
        )
    )
    save_task(tmp_path, active)
    save_task_runtime(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None: (
            _completed_subagent_result(tmp_path, task.pipeline_status, task=task)
        ),
    )

    summary = run_task(tmp_path, pending)

    assert summary.task is not None
    assert summary.task.id == pending.id
    assert summary.result is not None
    assert summary.result.final_status == "done"

    refreshed_active = require_task(tmp_path, active.id)
    assert refreshed_active.status == "interrupted"
    assert refreshed_active.pipeline_status == "testing"
    assert refreshed_active.runtime.execution_status == "interrupted"
    assert refreshed_active.runtime.interruption is not None
    assert refreshed_active.runtime.interruption.resume_stage == "testing"

    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert active.id in restored_state.queue


def test_cli_parser_has_no_duplicate_subcommands_or_arguments() -> None:
    """Catch duplicate subparser or argument definitions that crash argparse."""
    from litehive.cli.parser import build_parser
    # build_parser() raises ArgumentError if any subcommand or argument is duplicated
    parser = build_parser()
    assert parser is not None
