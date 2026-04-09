from tests.workspace_helpers import (
    LitehiveConfig,
    Path,
    argparse,
    build_parser,
    create_task,
    ensure_workspace,
    global_config_path,
    load_config,
    pytest,
    resolve_engine_name,
    resolve_engine_plan,
    resolve_execution_retry_policy,
    resolve_model,
    yaml,
)

import litehive.agents.quota.codex_quota as _codex_quota_mod


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


def test_health_parser_accepts_workspace_arg() -> None:
    parser = build_parser()

    args = parser.parse_args(["health", "--workspace", "/tmp/demo"])

    assert args.command == "health"
    assert args.workspace == Path("/tmp/demo")


def _assert_engine_status_command_shows_all_monitored_engines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.models import EngineUsageRecord, EngineUsageWindow, WorkspaceEngineMonitoring
    from litehive.observability._engine_monitoring import save_engine_monitoring
    from litehive.cli import _cmd_engine
    from litehive.agents.quota.claude_quota import ClaudeQuotaStatus
    from litehive.agents.quota.copilot_quota import CopilotQuotaStatus
    from litehive.agents.quota.zai_quota import ZaiQuotaStatus

    monkeypatch.setattr(
        "litehive.cli.engine.check_codex_quota",
        lambda: _codex_quota_mod.CodexQuotaStatus(error="test-disabled"),
    )
    monkeypatch.setattr(
        "litehive.agents.quota.claude_quota.check_claude_quota",
        lambda: ClaudeQuotaStatus(error="no-credentials"),
    )
    monkeypatch.setattr(
        "litehive.agents.quota.copilot_quota.check_copilot_quota",
        lambda: CopilotQuotaStatus(error="gh not on PATH"),
    )
    monkeypatch.setattr(
        "litehive.agents.quota.zai_quota.check_zai_quota",
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
    for expected in (
        "workspace: ",
        "engine: codex",
        "invocations: 7",
        "successes: 6",
        "failures: 1",
        "limits: 1",
        "last_used: 2026-04-08T22:10:00Z",
        "engine: gemini",
        "invocations: 3",
        "last_used: 2026-04-08T21:00:00Z",
        "=== live quota ===",
        "quota: unavailable (test-disabled)",
        "quota: unavailable (no-credentials)",
        "quota: unavailable (gh not on PATH)",
        "quota: unavailable (goz not on PATH)",
    ):
        assert expected in output


def test_engine_status_command_shows_all_monitored_engines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_engine_status_command_shows_all_monitored_engines(tmp_path, capsys, monkeypatch)


def test_engine_status_command_scopes_to_single_engine_and_shows_codex_quota(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))

    from litehive.cli import _cmd_engine
    from litehive.agents.quota.codex_quota import CodexQuotaStatus, CodexQuotaWindow

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
    from litehive.agents.quota.claude_quota import ClaudeQuotaStatus, ClaudeQuotaWindow

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
        "litehive.agents.quota.claude_quota.check_claude_quota",
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
    from litehive.agents.quota.copilot_quota import CopilotQuotaStatus

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
        "litehive.agents.quota.copilot_quota.check_copilot_quota",
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
    from litehive.agents.quota.zai_quota import ZaiQuotaStatus, ZaiQuotaWindow

    def fake_check_zai_quota():
        return ZaiQuotaStatus(
            limit_reached=True,
            api_calls=ZaiQuotaWindow(used_percent=81.0, window_hours=24, remaining=19, limit=100),
            tokens=ZaiQuotaWindow(used_percent=64.0, window_hours=24, remaining=360, limit=1000),
            checked_at=1.0,
        )

    monkeypatch.setattr(
        "litehive.agents.quota.zai_quota.check_zai_quota",
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
        "litehive.agents.quota.copilot_quota.check_copilot_quota",
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


def _assert_default_retry_policy(config: LitehiveConfig, engine_name: str) -> None:
    policy = config.execution_retry_policies[engine_name]
    assert policy.max_retries == 2
    assert policy.backoff_seconds == 0.25
    assert policy.backoff_multiplier == 2.0
    assert policy.retry_on == ["timeout", "network", "service"]


def test_litehive_config_defaults_include_claude_retry_policy() -> None:
    config = LitehiveConfig()

    assert config.subagent_inactivity_timeout_seconds == 360.0
    for engine_name in ("claude", "codex", "opencode", "gemini"):
        _assert_default_retry_policy(config, engine_name)
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
            "before_implementing=run:echo pre",
            "after_implementing=reject:echo post",
            "before_accepting=run:echo review",
            "after_accepting=run:echo accepted",
            "after_commit=reject:echo verify",
        ],
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)

    assert config.runner_hooks["before_implementing"][0].reject_on_failure is False
    assert config.runner_hooks["after_implementing"][0].command == "echo post"
    assert config.runner_hooks["after_implementing"][0].reject_on_failure is True
    assert config.runner_hook_execution_mode == "run_all"
    assert config.runner_hooks["before_accepting"][0].command == "echo review"
    assert config.runner_hooks["before_accepting"][0].reject_on_failure is False
    assert config.runner_hooks["after_accepting"][0].reject_on_failure is False
    assert config.runner_hooks["after_commit"][0].command == "echo verify"
    assert config.runner_hooks["after_commit"][0].reject_on_failure is True


def test_load_config_preserves_runner_hook_descriptions(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner_hooks": {
                    "after_implementing": [
                        {
                            "command": "uv run ruff check .",
                            "reject_on_failure": True,
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

    assert config.runner_hooks["after_implementing"][0].description == (
        "ensures lint passes before acceptance"
    )


def test_load_config_preserves_runner_hook_execution_mode(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump({"runner_hook_execution_mode": "fail_fast"}, sort_keys=False),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runner_hook_execution_mode == "fail_fast"


def test_load_config_rejects_invalid_runner_hook_execution_mode(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump({"runner_hook_execution_mode": "sometimes"}, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runner_hook_execution_mode must be one of:"):
        load_config(tmp_path)


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
        hook=["invalid_hook_point=run:echo nope"],
        subagent_resource_limits_enabled=None,
        subagent_memory_mb=None,
        subagent_cpu_count=None,
        subagent_process_limit=None,
    )

    assert _cmd_configure(parser) == 1
    output = capsys.readouterr().out

    assert "configure failed: runner_hooks key must be one of:" in output
