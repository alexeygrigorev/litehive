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
    SubagentResourceLimitsConfig,
    available_process_profiles,
    ensure_workspace,
    get_engine,
    load_config,
    load_engine_monitoring,
    record_engine_execution,
    render_context_template,
    resolve_process_profile,
)


def test_config_package_facade_re_exports_public_helpers() -> None:
    import litehive.config as config_module

    exported = {
        "LitehiveConfig": config_module.LitehiveConfig,
        "ensure_workspace": config_module.ensure_workspace,
        "load_config": config_module.load_config,
        "workspace_dir": config_module.workspace_dir,
        "config_path": config_module.config_path,
        "state_path": config_module.state_path,
        "render_workspace_gitignore": config_module.render_workspace_gitignore,
        "format_runner_hooks": config_module.format_runner_hooks,
    }

    assert exported["LitehiveConfig"].__module__ == "litehive.config.model"
    assert exported["ensure_workspace"].__module__ == "litehive.config.workspace"
    assert exported["load_config"].__module__ == "litehive.config.loading"
    assert exported["workspace_dir"].__module__ == "litehive.config.paths"
    assert exported["format_runner_hooks"].__module__ == "litehive.config.formatting"
    assert len((Path(config_module.__file__)).read_text(encoding="utf-8").splitlines()) < 50


def test_ensure_workspace_creates_layout(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    assert (tmp_path / ".litehive" / "config.yaml").exists()
    assert (tmp_path / ".litehive" / "state.yaml").exists()
    assert (tmp_path / ".litehive" / ".gitignore").exists()
    assert (tmp_path / ".litehive" / "tasks").exists()


def test_ensure_workspace_scaffolds_workspace_gitignore(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    gitignore = (tmp_path / ".litehive" / ".gitignore").read_text(encoding="utf-8")

    assert "logs/" in gitignore
    assert "engine-monitoring.yaml" in gitignore
    assert "tasks/*/runtime.yaml" in gitignore
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


def test_gemini_extract_usage_observation_reads_finished_usage_metadata(tmp_path: Path) -> None:
    adapter = get_engine("gemini")

    observation = adapter.extract_usage_observation(
        CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=tmp_path,
            exit_code=0,
            stdout=(
                '{"type":"Content","value":"done"}\n'
                '{"type":"Finished","value":{"reason":"STOP","usageMetadata":'
                '{"promptTokenCount":11,"candidatesTokenCount":7,"totalTokenCount":18}}}\n'
            ),
            stderr="",
        )
    )

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "google"
    assert observation.usage is not None
    assert observation.usage.used == 18
    assert observation.usage.unit == "tokens"
    assert observation.metadata["promptTokenCount"] == 11
    assert observation.metadata["candidatesTokenCount"] == 7
    assert observation.metadata["finish_reason"] == "STOP"


def test_gemini_extract_usage_observation_reads_result_stats_output(tmp_path: Path) -> None:
    adapter = get_engine("gemini")

    observation = adapter.extract_usage_observation(
        CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=tmp_path,
            exit_code=0,
            stdout=(
                '{"type":"init","model":"gemini-2.5-pro"}\n'
                '{"type":"result","status":"success","stats":{"total_tokens":18,'
                '"input_tokens":11,"output_tokens":7,"cached":3,"duration_ms":1200}}\n'
            ),
            stderr="",
        )
    )

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "google"
    assert observation.usage is not None
    assert observation.usage.used == 18
    assert observation.usage.unit == "tokens"
    assert observation.metadata["model"] == "gemini-2.5-pro"
    assert observation.metadata["input_tokens"] == 11
    assert observation.metadata["output_tokens"] == 7
    assert observation.metadata["cached_tokens"] == 3
    assert observation.metadata["duration_ms"] == 1200


def test_gemini_extract_usage_observation_reads_provider_limit_payload(tmp_path: Path) -> None:
    adapter = get_engine("gemini")

    observation = adapter.extract_usage_observation(
        CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=tmp_path,
            exit_code=1,
            stdout=(
                '{"type":"Error","value":{"message":"You exceeded your current quota, please check your plan and billing details. '
                'Please retry in 56s.","status":"RESOURCE_EXHAUSTED","details":['
                '{"@type":"type.googleapis.com/google.rpc.QuotaFailure","violations":[{'
                '"quotaMetric":"generativelanguage.googleapis.com/generate_content_free_tier_requests",'
                '"quotaId":"GenerateRequestsPerMinutePerProjectPerModel-FreeTier",'
                '"quotaDimensions":{"location":"global","model":"gemini-2.5-pro"},'
                '"quotaValue":"2"}]},'
                '{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"56s"}]}}\n'
            ),
            stderr="",
        )
    )

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "google"
    assert observation.success is False
    assert observation.limit_reason == "quota limit reached"
    assert observation.usage is not None
    assert observation.usage.limit == 2
    assert observation.usage.unit == "requests"
    assert observation.metadata["error_status"] == "RESOURCE_EXHAUSTED"
    assert observation.metadata["quota_metric"] == (
        "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    )
    assert (
        observation.metadata["quota_id"] == "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    )
    assert observation.metadata["quota_model"] == "gemini-2.5-pro"
    assert observation.metadata["retry_delay"] == "56s"
    assert observation.metadata["retry_delay_ms"] == 56000


def test_claude_extract_usage_observation_reads_result_usage_payload(tmp_path: Path) -> None:
    adapter = get_engine("claude")

    observation = adapter.extract_usage_observation(
        CLIExecutionResult(
            adapter="claude",
            argv=("claude", "-p"),
            cwd=tmp_path,
            exit_code=0,
            stdout=(
                '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}\n'
                '{"type":"result","subtype":"success","is_error":false,"duration_ms":1200,'
                '"total_cost_usd":0.0125,"usage":{"input_tokens":100,"output_tokens":40,'
                '"cache_creation_input_tokens":5,"cache_read_input_tokens":7,'
                '"service_tier":"priority"}}\n'
            ),
            stderr="",
        )
    )

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "anthropic"
    assert observation.usage is not None
    assert observation.usage.used == 152
    assert observation.usage.unit == "tokens"
    assert observation.metadata["input_tokens"] == 100
    assert observation.metadata["output_tokens"] == 40
    assert observation.metadata["service_tier"] == "priority"
    assert observation.metadata["total_cost_usd"] == "0.012500"


def test_claude_extract_usage_observation_reads_provider_limit_payload(tmp_path: Path) -> None:
    adapter = get_engine("claude")

    observation = adapter.extract_usage_observation(
        CLIExecutionResult(
            adapter="claude",
            argv=("claude", "-p"),
            cwd=tmp_path,
            exit_code=1,
            stdout=(
                '{"type":"error","error":{"type":"rate_limit_error","message":"Your account has hit a rate limit. '
                'Please retry after a short delay."}}\n'
            ),
            stderr="",
        )
    )

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "anthropic"
    assert observation.success is False
    assert observation.limit_reason == "rate limit reached"
    assert observation.metadata["error_type"] == "rate_limit_error"
    assert observation.metadata["error_message"] == (
        "Your account has hit a rate limit. Please retry after a short delay."
    )


def test_copilot_extract_usage_observation_reads_quota_snapshot(tmp_path: Path) -> None:
    adapter = get_engine("copilot")

    observation = adapter.extract_usage_observation(
        CLIExecutionResult(
            adapter="copilot",
            argv=("copilot", "-p"),
            cwd=tmp_path,
            exit_code=0,
            stdout=(
                '{"type":"assistant.usage","data":{"model":"gpt-5",'
                '"inputTokens":120,"outputTokens":30,"cost":2,'
                '"quotaSnapshots":{"premium_interactions":{"isUnlimitedEntitlement":false,'
                '"entitlementRequests":100,"usedRequests":60,'
                '"usageAllowedWithExhaustedQuota":false,"overage":0,'
                '"overageAllowedWithExhaustedQuota":false,'
                '"remainingPercentage":0.4,'
                '"resetDate":"2026-04-30T00:00:00Z"}}}}\n'
            ),
            stderr="",
        )
    )

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "github"
    assert observation.usage is not None
    assert observation.usage.used == 60
    assert observation.usage.limit == 100
    assert observation.usage.remaining == 40
    assert observation.usage.unit == "requests"
    assert observation.usage.reset_at == "2026-04-30T00:00:00Z"
    assert observation.metadata["quota_snapshot"] == "premium_interactions"
    assert observation.metadata["model"] == "gpt-5"


def test_ensure_workspace_scaffolds_profile_specific_context(tmp_path: Path) -> None:
    django_path = tmp_path / "django"
    django_path.mkdir()

    from litehive.config import LitehiveConfig

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
    assert [item.env_var for item in policy.credential_inputs] == ["GOOGLE_APPLICATION_CREDENTIALS"]


def test_native_process_profiles_expose_resource_limit_defaults() -> None:
    rust = LitehiveConfig(process_profile="rust")
    cpp = LitehiveConfig(process_profile="cpp")

    assert rust.subagent_resource_limits.enabled is True
    assert rust.subagent_resource_limits.memory_mb == 8192
    assert rust.subagent_resource_limits.cpu_count == 4.0
    assert rust.subagent_resource_limits.process_limit == 512
    assert cpp.subagent_resource_limits.enabled is True
    assert cpp.subagent_resource_limits.memory_mb == 12288
    assert cpp.subagent_resource_limits.cpu_count == 6.0
    assert cpp.subagent_resource_limits.process_limit == 1024


def test_workspace_resource_limit_overrides_replace_profile_defaults() -> None:
    config = LitehiveConfig(
        process_profile="rust",
        subagent_resource_limits=SubagentResourceLimitsConfig(
            enabled=True,
            memory_mb=2048,
            cpu_count=1.5,
            process_limit=96,
        ),
    )

    assert config.subagent_resource_limits.enabled is True
    assert config.subagent_resource_limits.memory_mb == 2048
    assert config.subagent_resource_limits.cpu_count == 1.5
    assert config.subagent_resource_limits.process_limit == 96


def test_available_process_profiles_include_generic_and_project_templates() -> None:
    assert available_process_profiles() == [
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
