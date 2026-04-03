import argparse
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import threading
import time

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import litehive.tasks as tasks_module

from litehive.cli import (
    _cmd_add,
    _cmd_intake,
    _cmd_abandon_task,
    _cmd_close_task,
    _cmd_move,
    _cmd_prioritize,
    _cmd_promote,
    _cmd_queue,
    _cmd_repair,
    _cmd_recover,
    _cmd_requeue_task,
    _cmd_resume_task,
    _cmd_rollback,
    _cmd_run,
    _cmd_stop_task,
    _cmd_status,
    _cmd_update,
    build_parser,
)
from litehive.engines import (
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
    get_engine,
)
from litehive.config import (
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    SandboxCredentialInput,
    SubagentResourceLimitsConfig,
    available_process_profiles,
    ensure_workspace,
    format_external_engine_sandbox,
    format_subagent_resource_limits,
    global_config_path,
    load_config,
    render_context_template,
    resolve_process_profile,
)
from litehive.engine_monitoring import (
    load_engine_monitoring,
    record_engine_execution,
)
from litehive.external_cli import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
    parse_stage_report_text,
)
from litehive.git_ops import GitError, checkpoint_message, commit_task
from litehive.models import (
    EngineUsageObservation,
    EngineUsageWindow,
    ResourceLimitEvent,
    RuntimeEngineContinuation,
    RuntimeInterruptionState,
    RuntimeStageState,
    RuntimeSubagentState,
    StageReport,
    SubagentRef,
    TaskRecord,
)
from litehive.observability import render_task_summary
from litehive.sandbox import SandboxLauncher
from litehive.runtime import (
    EngineBudgetLedger,
    TaskPoolStopConditions,
    _commit_to_git_report,
    _role_for_step,
    _allowed_commit_paths,
    _unexpected_dirty_paths,
    drain_task_pool,
    rollback_completed_task,
    resolve_engine_plan,
    recover_completed_task,
    resolve_execution_retry_policy,
    resolve_engine_name,
    resolve_model,
    resolve_next_task,
    run_next_task,
    run_single_task,
    run_task,
)
from litehive.runner import TaskExecutionRunner
from litehive.subagents import (
    EngineFailure,
    SubagentManager,
    SubagentResult,
    intake_prompt,
    stage_prompt,
    stage_report_from_subagent,
)
from litehive.tasks import (
    WorkspaceConflictError,
    abandon_task,
    close_task,
    create_task,
    dequeue_next_task_selection,
    finish_task_run_transition,
    get_task,
    implementation_entry_stage,
    list_tasks,
    load_state,
    move_queued_task,
    mark_subagent_started,
    peek_next_task_selection,
    repair_workspace_state,
    requeue_task,
    recover_stale_runner_state,
    resume_task,
    reroute_stage_for_acceptance_criteria,
    require_task,
    save_state,
    save_task,
    save_task_runtime,
    set_active_task,
    stop_current_task,
    restore_untouched_active_task,
    task_dir,
    task_file,
    task_runtime_file,
    task_requires_acceptance_criteria,
    update_task_metadata,
)


def _block_runner_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.tasks.fcntl.flock", fake_flock)


def _fail_atomic_write_on_path(
    monkeypatch: pytest.MonkeyPatch, failing_path: Path, message: str = "write failed"
) -> None:
    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_selected_write(path: Path, content: str) -> None:
        if path == failing_path:
            raise OSError(message)
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_selected_write)


def _latest_pool_run_report(root: Path) -> dict[str, object]:
    reports = sorted((root / ".litehive" / "logs" / "pool-runs").glob("*.yaml"))
    assert reports
    return yaml.safe_load(reports[-1].read_text(encoding="utf-8")) or {}


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
        def build_command(self, prompt: str, cwd: Path, model: str | None = None, *, max_turns: int | None = None) -> list[str]:  # type: ignore[override]
            return ["provider-cli", prompt]

        def extract_usage_observation(self, execution: CLIExecutionResult) -> EngineUsageObservation | None:
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
            capabilities=AdapterCapabilities(supports_model_override=True, transcript_format="jsonl"),
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
    assert observation.metadata["quota_id"] == "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
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

    assert "Process profile: Django" in context
    assert "## Init scaffold" in context
    assert "## Prompt scaffold" in context
    assert "## Stage prompt scaffolding" in context
    assert "## Django specifics" in context
    assert "migrations" in context
    assert (
        "Shared stages: grooming -> implementing -> testing -> accepting -> commit_to_git."
        in context
    )


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
    assert available_process_profiles() == ["codehive", "cpp", "django", "generic", "python", "rust"]


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
    assert profile["stage_overlay"]["accepting"][0].startswith("- Reviewer acceptance is managerial")


def test_render_context_template_shows_base_and_project_stage_scaffolding() -> None:
    context = render_context_template("rust")

    assert "## Stage prompt scaffolding" in context
    assert "### grooming" in context
    assert "### implementing" in context
    assert "### testing" in context
    assert "### accepting" in context
    assert "Apply stage defaults first, then append any project-specific stage overlay for that step." in context
    assert "Add or adjust focused Rust tests close to the changed crate or module." in context
    assert "Prefer targeted `cargo test`, `cargo check`, or package-scoped verification before workspace-wide runs." in context


def test_create_task_persists_folder_and_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Fix login race")
    tasks = list_tasks(tmp_path)
    state = load_state(tmp_path)

    assert task.id == "T-0001"
    assert len(tasks) == 1
    assert state.queue == ["T-0001"]
    assert (tmp_path / ".litehive" / "tasks" / "T-0001-fix-login-race" / "task.yaml").exists()


def test_save_task_rolls_back_task_record_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Atomic save", auto_commit=False)
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        save_task(tmp_path, task)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "idle"


def test_create_task_seeds_tasks_mode_template_defaults(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Investigate queue stalls", task_type="research", mode="tasks")
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")

    assert task.goal == "Answer the open question with concrete evidence and a recommendation for next action."
    assert task.acceptance_criteria == [
        "The research question, scope, and decision to inform are stated clearly.",
        "Findings are grounded in repository evidence, experiments, or direct inspection.",
        "The output includes a recommendation, tradeoffs, and any follow-up tasks.",
    ]
    assert task.constraints == [
        "Prefer evidence from the repository and local experiments over speculation.",
        "Keep conclusions explicit about confidence and remaining unknowns.",
    ]
    assert task.plan == [
        "Define the exact question and scope of the investigation.",
        "Gather evidence from code, configs, tests, or focused experiments.",
        "Summarize findings, recommendation, and concrete follow-up actions.",
    ]
    assert "## Template Guidance" in brief
    assert "Frame the question, scope, and decision this research should inform." in brief
    assert "## Intake Notes" in brief
    assert "### Question and Scope" in brief
    assert "Define what is being investigated and what is out of scope." in brief
    assert "_TBD_" in brief


@pytest.mark.parametrize(
    ("task_type", "title"),
    [
        ("adapter", "Add Gemini adapter"),
        ("bugfix", "Fix queue retry regression"),
        ("research", "Investigate queue stalls"),
        ("review", "Review adapter update"),
        ("refactor", "Refactor queue routing"),
    ],
)
def test_create_task_seeds_requested_task_type_templates(tmp_path: Path, task_type: str, title: str) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title=title, task_type=task_type, mode="tasks")
    template = tasks_module.TASK_TEMPLATES[task_type]
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")
    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert task.goal == template["goal"]
    assert task.acceptance_criteria == template["acceptance_criteria"]
    assert task.constraints == template["constraints"]
    assert task.plan == template["plan"]
    assert f"- Task type: {task_type}" in brief
    assert "## Template Guidance" in brief
    assert "## Intake Notes" in brief
    assert f"Task type: {task_type}" in prompt
    assert "Task template:" in prompt
    assert "Template sections to fill or verify:" in prompt

    for item in template["prompt_guidance"]:
        assert item in brief
        assert item in prompt
    for item in template["brief_sections"]:
        assert item in prompt
    for stub in template["brief_section_stubs"]:
        assert f"### {stub['title']}" in brief
        assert stub["prompt"] in brief


def test_intake_prompt_uses_codehive_style_guidance() -> None:
    prompt = intake_prompt("Need a rough task from this brain dump.")

    assert "You are the planner for a local multi-agent coding workspace." in prompt
    assert "You are handling freeform task intake for a Codehive-style workflow." in prompt
    assert "Preserve execution visibility through task reports, subagent transcripts, and recent progress." in prompt
    assert "Do not add acceptance criteria, implementation plans, decomposition, or detailed structure." in prompt
    assert "Treat the original dump as the authoritative source of detail." in prompt
    assert "TITLE: <concise rough task title>" in prompt
    assert "GOAL: <1-3 sentence high-level goal statement>" in prompt


def test_intake_command_creates_linked_task_from_freeform_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)

    captured: dict[str, object] = {}

    class FakeEngine:
        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            captured["prompt"] = prompt
            captured["cwd"] = cwd
            captured["model"] = model
            captured["max_turns"] = max_turns
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout="TITLE: Capture queue visibility gaps\nGOAL: Turn the raw notes into a queued task planner can groom later.\n",
                stderr="",
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.cli.get_engine", lambda _: FakeEngine())

    dump = "We need better queue visibility.\nShow stage, transcript, and last progress in the task view.\n"
    intake_file = tmp_path / "brain-dump.md"
    intake_file.write_text(dump, encoding="utf-8")

    exit_code = _cmd_intake(
        argparse.Namespace(
            file=intake_file,
            engine="opencode",
            model=None,
            workspace=tmp_path,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.title == "Capture queue visibility gaps"
    assert task.goal == (
        "Turn the raw notes into a queued task planner can groom later.\n\n"
        "(See intake.md for the original brain dump)"
    )
    assert task.mode == "tasks"
    assert task.task_type == "intake"
    assert task.status == "queued"
    assert task.pipeline_status == "backlog"
    assert captured["cwd"] == tmp_path
    assert captured["model"] == "zai-coding-plan/glm-5.1"
    assert captured["max_turns"] is None
    assert "Codehive-style specifics:" in str(captured["prompt"])

    base = task_dir(tmp_path, task)
    assert (base / "intake.md").read_text(encoding="utf-8") == dump
    brief = (base / "brief.md").read_text(encoding="utf-8")
    assert "- Original dump: [intake.md](intake.md)" in brief
    assert "Treat `intake.md` as the authoritative source for the raw specification." in brief
    assert dump.strip() not in brief
    assert "Created task T-0001: Capture queue visibility gaps" in output
    assert "Original dump preserved at:" in output


def test_update_task_fills_only_unset_template_fields_for_typed_tasks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Review queue behavior",
        task_type="review",
        mode="tasks",
        goal="Use explicit review framing",
        acceptance_criteria=["Call out the highest-risk regression first."],
    )

    updated = tasks_module.update_task(tmp_path, task.id, mode="tasks", task_type="review")

    assert updated.goal == "Use explicit review framing"
    assert updated.acceptance_criteria == ["Call out the highest-risk regression first."]
    assert updated.constraints == tasks_module.TASK_TEMPLATES["review"]["constraints"]
    assert updated.plan == tasks_module.TASK_TEMPLATES["review"]["plan"]


def test_create_task_preserves_explicit_fields_when_seeding_template_defaults(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(
        tmp_path,
        title="Stabilize flaky queue retry",
        task_type="bugfix",
        mode="tasks",
        goal="Eliminate the duplicate retry path",
        acceptance_criteria=["Queue retries once for a limit error"],
    )

    assert task.goal == "Eliminate the duplicate retry path"
    assert task.acceptance_criteria == ["Queue retries once for a limit error"]
    assert task.constraints
    assert task.plan


def test_create_task_persists_dependencies(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")
    dependent = create_task(
        tmp_path,
        title="Dependent task",
        depends_on=[first.id, second.id],
    )

    persisted = get_task(tmp_path, dependent.id)

    assert persisted is not None
    assert persisted.depends_on == [first.id, second.id]


def test_subagent_artifacts_exist_while_engine_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Persist live subagent artifacts")
    manager = SubagentManager(tmp_path)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            assert max_turns is None
            assert on_started is not None
            on_started(4242)
            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            assert base.exists()
            session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
            assert session["id"] == "SA-0001"
            assert session["role"] == "swe"
            assert session["engine"] == "codex"
            assert session["status"] == "running"
            assert session["created_at"]
            assert session["updated_at"]
            assert session["pid"] == 4242
            assert session["exit_code"] is None
            assert (base / "prompt.txt").read_text(encoding="utf-8") == prompt
            assert (base / "transcript.md").read_text(encoding="utf-8") == ""
            assert (base / "stdout.txt").read_text(encoding="utf-8") == ""
            assert (base / "stderr.txt").read_text(encoding="utf-8") == ""
            report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
            assert report["status"] == "running"
            assert report["summary"] == ""
            refreshed = get_task(tmp_path, task.id)
            assert refreshed is not None
            assert refreshed.runtime.active_subagent is not None
            assert refreshed.runtime.active_subagent.path == "subagents/SA-0001-swe"
            assert refreshed.runtime.active_subagent.pid == 4242
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout=(
                    "VERDICT: PASS\n"
                    "SUMMARY: artifacts persisted live\n"
                    "FILES_CHANGED:\n"
                    "- litehive/subagents.py\n"
                    "TESTS_ADDED: 1\n"
                    "TESTS_PASSING: 1\n"
                    "WARNINGS:\n"
                    "- none\n"
                ),
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["id"] == "SA-0001"
    assert session["role"] == "swe"
    assert session["engine"] == "codex"
    assert session["status"] == "completed"
    assert session["created_at"]
    assert session["updated_at"]
    assert session["pid"] == 4242
    assert session["exit_code"] == 0
    assert (base / "transcript.md").read_text(encoding="utf-8") == (
        "VERDICT: PASS\n"
        "SUMMARY: artifacts persisted live\n"
        "FILES_CHANGED:\n"
        "- litehive/subagents.py\n"
        "TESTS_ADDED: 1\n"
        "TESTS_PASSING: 1\n"
        "WARNINGS:\n"
        "- none\n"
    )
    assert (base / "stdout.txt").read_text(encoding="utf-8") == (
        "VERDICT: PASS\n"
        "SUMMARY: artifacts persisted live\n"
        "FILES_CHANGED:\n"
        "- litehive/subagents.py\n"
        "TESTS_ADDED: 1\n"
        "TESTS_PASSING: 1\n"
        "WARNINGS:\n"
        "- none\n"
    )
    assert (base / "stderr.txt").read_text(encoding="utf-8") == ""
    assert report == {
        "status": "completed",
        "summary": "artifacts persisted live",
        "files_changed": ["litehive/subagents.py"],
        "tests": {"added": 1, "passing": 1},
        "warnings": ["none"],
        "interruption_reason": None,
        "continuation": None,
        "resource_control": {
            "enabled": False,
            "runtime": None,
            "image": None,
            "network_mode": None,
            "workspace_mode": None,
            "memory_mb": None,
            "cpu_count": None,
            "process_limit": None,
            "environment": [],
            "credential_inputs": [],
        },
        "resource_limit_event": None,
    }
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.pid == 4242
    monitoring = load_engine_monitoring(tmp_path)
    assert monitoring.engines["codex"].invocation_count == 1
    assert monitoring.engines["codex"].success_count == 1


def test_subagent_artifacts_update_live_during_streaming_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stream live subagent artifacts")
    manager = SubagentManager(tmp_path)

    class FakeStreamingEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_update=None,
        ) -> CLIExecutionResult:
            first = CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: streaming",
                stderr="partial stderr",
                pid=5151,
            )
            assert on_update is not None
            on_update(first)

            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
            report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
            assert session["created_at"]
            assert session["updated_at"]
            assert session["status"] == "running"
            assert session["pid"] == 5151
            assert session["exit_code"] is None
            assert (base / "stdout.txt").read_text(encoding="utf-8") == "VERDICT: PASS\nSUMMARY: streaming"
            assert (base / "stderr.txt").read_text(encoding="utf-8") == "partial stderr"
            assert (base / "transcript.md").read_text(encoding="utf-8") == (
                "VERDICT: PASS\nSUMMARY: streaming\n\n[stderr]\npartial stderr"
            )
            assert report["status"] == "running"
            assert report["summary"] == "streaming"

            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout=(
                    "VERDICT: PASS\n"
                    "SUMMARY: streaming complete\n"
                    "FILES_CHANGED:\n"
                    "- litehive/external_cli.py\n"
                ),
                stderr="",
                pid=5151,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeStreamingEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="stream it")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "completed"
    assert session["created_at"]
    assert session["updated_at"]
    assert session["pid"] == 5151
    assert session["exit_code"] == 0
    assert (base / "stdout.txt").read_text(encoding="utf-8") == (
        "VERDICT: PASS\n"
        "SUMMARY: streaming complete\n"
        "FILES_CHANGED:\n"
        "- litehive/external_cli.py\n"
    )
    assert report["summary"] == "streaming complete"
    assert report["files_changed"] == ["litehive/external_cli.py"]
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.pid == 5151


def test_subagent_manager_records_copilot_quota_monitoring_during_live_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stream Copilot quota usage")
    manager = SubagentManager(tmp_path)
    adapter = get_engine("copilot")

    def fake_run_live(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
        on_update=None,
    ) -> CLIExecutionResult:
        del prompt, model, max_turns
        if on_started is not None:
            on_started(6262)
        update = CLIExecutionResult(
            adapter="copilot",
            argv=("copilot", "-p"),
            cwd=cwd,
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
            pid=6262,
        )
        assert on_update is not None
        on_update(update)

        monitoring = load_engine_monitoring(tmp_path)
        record = monitoring.engines["copilot"]
        assert record.source == "provider"
        assert record.provider == "github"
        assert record.invocation_count == 0
        assert record.success_count == 0
        assert record.failure_count == 0
        assert record.usage is not None
        assert record.usage.used == 60
        assert record.usage.remaining == 40
        assert record.usage.reset_at == "2026-04-30T00:00:00Z"
        assert record.metadata["quota_snapshot"] == "premium_interactions"

        return update

    monkeypatch.setattr(adapter, "run_live", fake_run_live)
    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: adapter)

    result = manager.run(task, role="swe", engine_name="copilot", prompt="monitor quota")

    assert result.ref.status == "completed"
    monitoring = load_engine_monitoring(tmp_path)
    record = monitoring.engines["copilot"]
    assert record.invocation_count == 1
    assert record.success_count == 1
    assert record.failure_count == 0
    assert record.usage is not None
    assert record.usage.used == 60
    assert record.usage.remaining == 40


def test_subagent_artifacts_stream_to_disk_while_process_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tail live subagent artifacts")
    manager = SubagentManager(tmp_path)

    class TestAdapter(ExternalCLIAdapter):
        def __init__(self) -> None:
            super().__init__(
                name="codex",
                binary="bash",
                capabilities=AdapterCapabilities(available=True),
            )

        def is_available(self) -> bool:
            return True

        def build_command(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
        ) -> list[str]:
            del prompt, cwd, model, max_turns
            return [
                "bash",
                "-lc",
                "printf 'VERDICT: PASS\\nSUMMARY: live start\\n'; "
                "printf 'live stderr\\n' >&2; "
                "sleep 1.2; "
                "printf 'FILES_CHANGED:\\n- litehive/external_cli.py\\nTESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n';",
            ]

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: TestAdapter())

    result_holder: dict[str, SubagentResult] = {}
    error_holder: list[BaseException] = []

    def run_manager() -> None:
        try:
            result_holder["result"] = manager.run(task, role="swe", engine_name="codex", prompt="stream it")
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            error_holder.append(exc)

    worker = threading.Thread(target=run_manager)
    worker.start()

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    deadline = time.time() + 5
    while time.time() < deadline:
        if base.exists() and (base / "stdout.txt").exists() and (base / "stderr.txt").exists():
            stdout_text = (base / "stdout.txt").read_text(encoding="utf-8")
            stderr_text = (base / "stderr.txt").read_text(encoding="utf-8")
            if "SUMMARY: live start" in stdout_text and "live stderr" in stderr_text:
                break
        time.sleep(0.05)
    else:
        worker.join(timeout=0)
        raise AssertionError("live stdout was not persisted before the process exited")

    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    stdout_text = (base / "stdout.txt").read_text(encoding="utf-8")
    stderr_text = (base / "stderr.txt").read_text(encoding="utf-8")
    transcript_text = (base / "transcript.md").read_text(encoding="utf-8")

    assert worker.is_alive()
    assert session["status"] == "running"
    assert session["pid"] is not None
    assert session["exit_code"] is None
    assert "SUMMARY: live start" in stdout_text
    assert "live stderr" in stderr_text
    assert "SUMMARY: live start" in transcript_text
    assert report["status"] == "running"
    assert report["summary"] == "live start"

    worker.join(timeout=5)
    assert not error_holder
    assert "result" in result_holder
    assert result_holder["result"].ref.status == "completed"

    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "completed"
    assert session["exit_code"] == 0
    assert report["summary"] == "live start"
    assert report["files_changed"] == ["litehive/external_cli.py"]
    assert report["tests"] == {"added": 1, "passing": 1}


def test_subagent_manager_avoids_existing_folder_collisions_for_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Avoid subagent folder collisions")
    manager = SubagentManager(tmp_path)

    stale_base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    stale_base.mkdir(parents=True, exist_ok=False)
    task.subagents.append(
        SubagentRef(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="failed",
            path="subagents/SA-0001-swe",
        )
    )
    save_task(tmp_path, task)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            base = task_dir(tmp_path, task) / "subagents" / "SA-0002-swe"
            assert base.exists()
            assert not (task_dir(tmp_path, task) / "subagents" / "SA-0003-swe").exists()
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: retry folder allocated safely\nFILES_CHANGED:\nTESTS_ADDED: 0\nTESTS_PASSING: 0\nWARNINGS:\n",
                stderr="",
                pid=7171,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="retry safely")

    assert result.ref.id == "SA-0002"
    assert result.ref.path == "subagents/SA-0002-swe"
    assert (task_dir(tmp_path, task) / "subagents" / "SA-0002-swe").exists()


def test_subagent_streaming_pid_persists_before_first_live_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Persist silent streaming pid")
    manager = SubagentManager(tmp_path)

    class SilentStreamingEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
            on_update=None,
        ) -> CLIExecutionResult:
            assert max_turns is None
            assert on_started is not None
            on_started(6161)

            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
            assert session["status"] == "running"
            assert session["pid"] == 6161
            assert session["exit_code"] is None
            assert (base / "transcript.md").read_text(encoding="utf-8") == ""
            assert (base / "stdout.txt").read_text(encoding="utf-8") == ""
            assert (base / "stderr.txt").read_text(encoding="utf-8") == ""

            refreshed = get_task(tmp_path, task.id)
            assert refreshed is not None
            assert refreshed.runtime.active_subagent is not None
            assert refreshed.runtime.active_subagent.pid == 6161

            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: silent streaming complete\n",
                stderr="",
                pid=6161,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: SilentStreamingEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="stream it silently")

    assert result.ref.status == "completed"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "completed"
    assert session["pid"] == 6161
    assert session["exit_code"] == 0

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.pid == 6161


def test_subagent_artifacts_capture_sandbox_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            external_engine_sandbox=ExternalEngineSandboxConfig(
                enabled=True,
                image="ghcr.io/example/litehive-sandbox:latest",
                engine_policies={
                    "codex": ExternalEngineSandboxPolicy(
                        enabled=True,
                        network_mode="none",
                        workspace_mode="rw",
                        environment=["OPENAI_API_KEY"],
                    )
                },
            )
        ),
    )
    task = create_task(tmp_path, title="Persist sandbox metadata")
    manager = SubagentManager(tmp_path)
    calls: dict[str, object] = {}

    class FakePopen:
        def __init__(self, cmd, cwd, env, stdout, stderr, text):  # type: ignore[no-untyped-def]
            calls["cmd"] = cmd
            calls["cwd"] = cwd
            calls["env"] = env
            self.pid = 7272
            self.returncode = 0
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            os.write(
                stdout_write,
                (
                    "VERDICT: PASS\nSUMMARY: sandboxed execution\nFILES_CHANGED:\n"
                    "- litehive/sandbox.py\nTESTS_ADDED: 1\nTESTS_PASSING: 1\nWARNINGS:\n"
                ).encode("utf-8"),
            )
            os.close(stdout_write)
            os.close(stderr_write)
            self.stdout = os.fdopen(stdout_read, "rb")
            self.stderr = os.fdopen(stderr_read, "rb")

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def wait(self):  # type: ignore[no-untyped-def]
            return self.returncode

    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setattr("litehive.subagents._supports_live_on_started", lambda engine: False)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert result.execution is not None
    assert result.execution.sandboxed is True
    assert "sandbox[" in result.execution.sandbox_summary
    assert "--env OPENAI_API_KEY=secret" in " ".join(calls["cmd"])
    assert "ANTHROPIC_API_KEY" not in " ".join(calls["cmd"])

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    assert session["sandboxed"] is True
    assert session["sandbox"].startswith("sandbox[")

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.sandboxed is True
    assert refreshed.runtime.last_subagent.sandbox_summary.startswith("sandbox[")


def test_subagent_artifacts_capture_structured_resource_limit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            process_profile="rust",
            subagent_resource_limits=SubagentResourceLimitsConfig(
                enabled=True,
                memory_mb=4096,
                cpu_count=2.0,
                process_limit=256,
            ),
        ),
    )
    task = create_task(tmp_path, title="Persist resource limit event")
    manager = SubagentManager(tmp_path)

    class FakePopen:
        def __init__(self, cmd, cwd, env, stdout, stderr, text):  # type: ignore[no-untyped-def]
            self.pid = 8181
            self.returncode = 137
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            os.write(stdout_write, b"native build aborted")
            os.write(stderr_write, b"OOMKilled: container exceeded memory limit")
            os.close(stdout_write)
            os.close(stderr_write)
            self.stdout = os.fdopen(stdout_read, "rb")
            self.stderr = os.fdopen(stderr_read, "rb")

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def wait(self):  # type: ignore[no-untyped-def]
            return self.returncode

    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setattr("litehive.subagents._supports_live_on_started", lambda engine: False)

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert result.failure is not None
    assert result.failure.kind == "resource_limit"
    assert result.failure.resource_limit_event is not None
    assert result.failure.resource_limit_event.resource == "memory"
    assert result.failure.resource_limit_event.memory_mb == 4096

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["resource_control"]["memory_mb"] == 4096
    assert session["resource_control"]["cpu_count"] == 2.0
    assert session["resource_control"]["process_limit"] == 256
    assert session["resource_limit_event"]["resource"] == "memory"
    assert report["resource_control"]["enabled"] is True
    assert report["resource_control"]["runtime"] == "docker"
    assert report["resource_limit_event"]["reason"] == "memory limit exceeded (OOM)"

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.resource_limit_event is not None
    assert refreshed.runtime.last_subagent.resource_limit_event.reason == "memory limit exceeded (OOM)"


def test_subagent_manager_marks_signal_terminated_execution_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Halted subagent execution")
    manager = SubagentManager(tmp_path)

    class InterruptedEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            assert max_turns is None
            if on_started is not None:
                on_started(7171)
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=130,
                stdout="Execution interrupted by user",
                stderr="received SIGINT",
                pid=7171,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: InterruptedEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="resume safely")

    assert result.ref.status == "interrupted"
    assert result.failure is not None
    assert result.failure.kind == "execution_interrupted"
    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    session = yaml.safe_load((base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "interrupted"
    assert session["pid"] == 7171
    assert session["interruption_reason"] == "execution interrupted"
    assert report["status"] == "interrupted"
    assert report["interruption_reason"] == "execution interrupted"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.status == "interrupted"
    assert refreshed.runtime.last_subagent.pid == 7171
    assert refreshed.runtime.last_subagent.interruption_reason == "execution interrupted"


def test_subagent_manager_uses_inherited_run_live_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task")
    manager = SubagentManager(tmp_path)
    engine = get_engine("codex")

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: engine)
    monkeypatch.setattr(engine, "is_available", lambda: True)

    calls: list[str] = []

    def fail_run(*args, **kwargs) -> CLIExecutionResult:  # type: ignore[no-untyped-def]
        raise AssertionError("run should not be used when run_live is available")

    def fake_run_live(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
        on_update=None,
    ) -> CLIExecutionResult:
        assert max_turns is None
        calls.append("run_live")
        assert on_started is not None
        on_started(4242)
        assert on_update is not None
        on_update(
            CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
                pid=4242,
            )
        )
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
            pid=4242,
        )

    monkeypatch.setattr("litehive.external_cli.ExternalCLIAdapter.run", fail_run)
    monkeypatch.setattr("litehive.external_cli.ExternalCLIAdapter.run_live", fake_run_live)

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert calls == ["run_live"]
    assert result.failure == EngineFailure(kind="execution_limit", reason="usage limit reached")


def test_subagent_manager_prefers_instance_run_override_over_inherited_run_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task")
    manager = SubagentManager(tmp_path)
    engine = get_engine("codex")

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: engine)
    monkeypatch.setattr(engine, "is_available", lambda: True)

    calls: list[str] = []

    def fake_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        calls.append("run")
        assert on_started is not None
        on_started(4242)
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
            pid=4242,
        )

    def fail_run_live(*args, **kwargs) -> CLIExecutionResult:  # type: ignore[no-untyped-def]
        raise AssertionError("run_live should not be used when only run is overridden")

    monkeypatch.setattr(engine, "run", fake_run)
    monkeypatch.setattr("litehive.external_cli.ExternalCLIAdapter.run_live", fail_run_live)

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert calls == ["run"]
    assert result.failure == EngineFailure(kind="execution_limit", reason="usage limit reached")


def test_create_task_rejects_missing_dependency(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with pytest.raises(ValueError, match="Task T-9999 not found"):
        create_task(tmp_path, title="Dependent task", depends_on=["T-9999"])


def test_create_task_rejects_dependency_cycle(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    first.depends_on = [second.id]
    save_task(tmp_path, first)

    with pytest.raises(
        ValueError, match=rf"Task {second.id} dependency cycle detected via {first.id}"
    ):
        update_task_metadata(tmp_path, second.id, depends_on=[first.id])


def test_runner_advances_task_to_done(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Implement feature")

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    reports = tmp_path / ".litehive" / "tasks" / "T-0001-implement-feature" / "reports"
    assert (reports / "grooming-001.yaml").exists()
    assert (reports / "implementing-002.yaml").exists()
    assert (reports / "testing-003.yaml").exists()
    assert (reports / "accepting-004.yaml").exists()
    assert (reports / "commit_to_git-005.yaml").exists()


def test_runtime_routes_grooming_to_planner_and_accepting_to_reviewer() -> None:
    assert _role_for_step("grooming") == "planner"
    assert _role_for_step("implementing") == "swe"
    assert _role_for_step("testing") == "qa"
    assert _role_for_step("accepting") == "reviewer"


def test_runtime_routes_flagged_and_interrupted_retries_to_recovery(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Flagged task")
    flagged.status = "flagged"
    flagged.pipeline_status = "implementing"
    flagged.runtime.last_outcome.kind = "flagged"
    save_task(tmp_path, flagged)

    interrupted = create_task(tmp_path, title="Halted task")
    interrupted.status = "interrupted"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.last_outcome.kind = "interrupted"
    save_task(tmp_path, interrupted)

    assert _role_for_step("implementing", require_task(tmp_path, flagged.id)) == "recovery"
    assert _role_for_step("testing", require_task(tmp_path, interrupted.id)) == "recovery"
    assert _role_for_step("grooming", require_task(tmp_path, interrupted.id)) == "planner"


def test_subagent_manager_persists_planner_and_reviewer_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Role split task")

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            del model, max_turns
            if on_started is not None:
                on_started(4242)
            step = prompt.split("Stage: ", 1)[1].splitlines()[0]
            return _stage_subagent_result(cwd, step).execution  # type: ignore[return-value]

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.subagents._supports_live_execution", lambda engine: False)
    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeEngine())
    manager = SubagentManager(tmp_path)

    planner_result = manager.run(task, role="planner", engine_name="codex", prompt="Stage: grooming")
    task = require_task(tmp_path, task.id)
    reviewer_result = manager.run(task, role="reviewer", engine_name="codex", prompt="Stage: accepting")
    task = require_task(tmp_path, task.id)

    assert planner_result.failure is None
    assert reviewer_result.failure is None
    assert [ref.role for ref in task.subagents] == ["planner", "reviewer"]
    assert task.subagents[0].path.endswith("-planner")
    assert task.subagents[-1].path.endswith("-reviewer")


def test_runner_requeues_task_after_testing_rejection(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(tmp_path, title="Review loop")

    def executor(task, step):  # type: ignore[no-untyped-def]
        verdict = "fail" if step == "testing" else "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}")

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.runtime.retry_count == 1
    assert task.runtime.retry_limit == 3
    assert task.runtime.retry_source == "global"


def test_runner_rejects_workflow_testing_without_real_lifecycle_evidence(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(
        tmp_path,
        title="Enforce workflow verification",
        goal="Prove control-plane lifecycle behavior through the real CLI",
        acceptance_criteria=[
            "commit_to_git succeeds and records the final checkpoint commit",
            "An interrupted task resumes from the correct stage in a real workspace run",
            "run-all behavior is proven through the wrapper or real CLI execution",
        ],
        auto_commit=False,
    )

    report = StageReport(
        task_id=task.id,
        step="testing",
        verdict="pass",
        summary="testing ok",
        feedback=(
            "Verified with pytest tests/test_workspace.py::test_commit_to_git_treats_clean_task_worktree_as_done "
            "and direct helper coverage around drain_task_pool."
        ),
    )

    runner = TaskExecutionRunner(tmp_path, lambda task, step: report, max_retries=3)
    enforced = runner._enforce_workflow_verification(task, "testing", report)

    assert enforced.verdict == "reject"
    assert enforced.summary == "testing rejected: missing required real lifecycle verification evidence"
    assert any("real Litehive CLI or wrapper lifecycle evidence" in warning for warning in enforced.warnings)
    assert any("final checkpoint commit" in warning for warning in enforced.warnings)
    assert any("correct stage" in warning for warning in enforced.warnings)


def test_runner_rejects_workflow_testing_without_clean_completion_record(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Verify workflow lifecycle",
        goal="Prove control-plane lifecycle behavior through the real CLI",
        acceptance_criteria=[
            "commit_to_git succeeds and records the final checkpoint commit",
            "An interrupted task resumes from the correct stage in a real workspace run",
        ],
        auto_commit=False,
    )

    evidence = """
Ran `uv run litehive run --workspace .` in a proof workspace.
Confirmed commit_to_git succeeded, recorded the final checkpoint commit, and checked the commit_sha with `git rev-parse HEAD`.
Also resumed with `uv run litehive resume T-0001 --workspace .` after an interruption at testing.
""".strip()

    report = StageReport(
        task_id=task.id,
        step="testing",
        verdict="pass",
        summary="testing ok",
        feedback=evidence,
    )

    runner = TaskExecutionRunner(tmp_path, lambda task, step: report)
    enforced = runner._enforce_workflow_verification(task, "testing", report)

    assert enforced.verdict == "reject"
    assert any("clean completion" in warning for warning in enforced.warnings)


def test_runner_accepts_workflow_testing_with_real_lifecycle_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Verify workflow lifecycle",
        goal="Prove control-plane lifecycle behavior through the real CLI",
        acceptance_criteria=[
            "commit_to_git succeeds and records the final checkpoint commit",
            "An interrupted task resumes from the correct stage in a real workspace run",
            "run-all behavior is proven through the wrapper or real CLI execution",
        ],
        auto_commit=False,
    )

    commit_workspace = tmp_path / "proof-commit"
    commit_workspace.mkdir()
    _init_git_repo(commit_workspace)
    ensure_workspace(commit_workspace)
    create_task(commit_workspace, title="Ship example change")

    resume_workspace = tmp_path / "proof-resume"
    resume_workspace.mkdir()
    ensure_workspace(resume_workspace, LitehiveConfig(auto_commit=False))
    create_task(resume_workspace, title="Finish example change", auto_commit=False)

    resume_once = {"seen": False}

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "implementing" and (
            self.execution_root == commit_workspace or commit_workspace in self.execution_root.parents
        ):
            app_path = self.execution_root / "app.txt"
            if app_path.exists():
                app_path.write_text("proof commit lifecycle\n", encoding="utf-8")
        if current_task.pipeline_status == "testing" and (
            self.execution_root == resume_workspace or resume_workspace in self.execution_root.parents
        ):
            if not resume_once["seen"]:
                resume_once["seen"] = True
                raise KeyboardInterrupt()
        return _completed_subagent_result(self.execution_root, current_task.pipeline_status, engine_name=engine_name)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    assert _cmd_run(argparse.Namespace(workspace=commit_workspace, dry_run=False, drain=False)) == 0
    commit_output = capsys.readouterr().out
    assert "status: done" in commit_output
    assert "commit:" in commit_output
    assert "commit_to_git=pass" in commit_output

    assert _cmd_run(argparse.Namespace(workspace=resume_workspace, dry_run=False, drain=False)) == 0
    interrupted_output = capsys.readouterr().out
    assert "status: interrupted" in interrupted_output

    assert (
        _cmd_resume_task(
            argparse.Namespace(workspace=resume_workspace, task_id="T-0001", front=True)
        )
        == 0
    )
    resume_output = capsys.readouterr().out
    assert "pipeline_status: testing" in resume_output

    assert _cmd_run(argparse.Namespace(workspace=resume_workspace, dry_run=False, drain=False)) == 0
    resumed_output = capsys.readouterr().out
    assert "status: done" in resumed_output

    wrapper_workspace = tmp_path / "proof-wrapper"
    wrapper_workspace.mkdir()
    (wrapper_workspace / ".litehive").mkdir()
    counts_dir = tmp_path / "proof-wrapper-counts"
    counts_dir.mkdir()
    status_count_file = counts_dir / "status-count"
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  if [[ "$count" -eq 1 ]]; then
    cat > "{wrapper_workspace / '.litehive' / 'state.yaml'}" <<'STATE'
active_task_id: null
queue:
  - T-0001
pool_stop_reason: null
STATE
  else
    cat > "{wrapper_workspace / '.litehive' / 'state.yaml'}" <<'STATE'
active_task_id: null
queue: []
pool_stop_reason: queue_exhausted
STATE
  fi
  echo "tasks_run: 1"
  echo "stop_reason: queue_exhausted"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )
    (wrapper_workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue:\n  - T-0001\npool_stop_reason: null\n",
        encoding="utf-8",
    )
    wrapper_result = subprocess.run(
        ["bash", str(_repo_root() / "scripts" / "run-all.sh"), str(wrapper_workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv),
        check=False,
    )
    assert wrapper_result.returncode == 0
    assert "== iteration 1 ==" in wrapper_result.stdout
    assert "No active or queued tasks remain. Stopping." in wrapper_result.stdout

    evidence = "\n\n".join(
        [
            "$ uv run litehive run --workspace .\n" + commit_output.strip(),
            "$ uv run litehive run --workspace .\n" + interrupted_output.strip(),
            "$ uv run litehive resume T-0001 --workspace .\n" + resume_output.strip(),
            "$ uv run litehive run --workspace .\n" + resumed_output.strip(),
            "$ bash scripts/run-all.sh .\n" + wrapper_result.stdout.strip(),
        ]
    )

    runner = TaskExecutionRunner(
        tmp_path,
        lambda task, step: StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok"),
    )
    testing_report = runner._enforce_workflow_verification(
        task,
        "testing",
        StageReport(
            task_id=task.id,
            step="testing",
            verdict="pass",
            summary="testing ok",
            feedback=evidence,
        ),
    )
    assert testing_report.verdict == "pass"
    reports_dir = task_dir(tmp_path, task) / "reports"
    reports_dir.mkdir(exist_ok=True)
    (reports_dir / "testing-001.yaml").write_text(
        yaml.safe_dump(testing_report.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    accepting_report = runner._enforce_workflow_verification(
        task,
        "accepting",
        StageReport(
            task_id=task.id,
            step="accepting",
            verdict="pass",
            summary="accepting ok",
            feedback="PM reviewed the real CLI evidence.",
        ),
    )
    assert accepting_report.verdict == "pass"


def test_runner_rejects_acceptance_when_testing_report_lacks_required_workflow_evidence(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(
        tmp_path,
        title="Accept workflow lifecycle evidence",
        goal="Only accept lifecycle claims that QA proved through the real CLI",
        acceptance_criteria=[
            "commit_to_git succeeds and records the final checkpoint commit",
            "An interrupted task resumes from the correct stage in a real workspace run",
        ],
        auto_commit=False,
    )
    task.pipeline_status = "accepting"
    save_task(tmp_path, task)

    (task_dir(tmp_path, task) / "reports" / "testing-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "testing",
                "verdict": "pass",
                "summary": "helper tests passed",
                "feedback": "Verified with pytest and direct helper-function tests only.",
                "files_changed": ["litehive/runner.py"],
                "tests": {"added": 1, "passing": 1},
                "warnings": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    runner = TaskExecutionRunner(
        tmp_path,
        lambda task, step: StageReport(
            task_id=task.id,
            step=step,
            verdict="pass",
            summary="accepting ok",
            feedback="PM reviewed the task summary and found it aligned.",
        ),
        max_retries=3,
    )
    enforced = runner._enforce_workflow_verification(
        task,
        "accepting",
        StageReport(
            task_id=task.id,
            step="accepting",
            verdict="pass",
            summary="accepting ok",
            feedback="PM reviewed the task summary and found it aligned.",
        ),
    )

    assert enforced.verdict == "reject"
    assert enforced.summary == "accepting rejected: QA evidence does not prove the claimed lifecycle behavior"
    assert any("real Litehive CLI or wrapper lifecycle evidence" in warning for warning in enforced.warnings)


def test_runner_persists_non_blocking_follow_up_and_completes_current_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ship feature behavior")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "accepting":
            return StageReport(
                task_id=task.id,
                step=step,
                verdict="pass",
                summary="acceptance passed with separate follow-up",
                follow_up_tasks=[
                    {
                        "title": "Document follow-up feature behavior",
                        "rationale": "Acceptance found documentation work that does not block shipment.",
                        "blocking": False,
                        "task_type": "research",
                    }
                ],
            )
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.runtime.execution_status == "done"
    follow_up = get_task(tmp_path, "T-0002")
    assert follow_up is not None
    assert follow_up.status == "queued"
    assert follow_up.pipeline_status == "backlog"
    assert follow_up.created_from is not None
    assert follow_up.created_from.task_id == task.id
    assert follow_up.created_from.stage == "accepting"
    assert follow_up.created_from.blocking is False
    assert (
        follow_up.created_from.rationale
        == "Acceptance found documentation work that does not block shipment."
    )
    assert load_state(tmp_path).queue == [follow_up.id]

    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / f"{task.id}-{task.slug}"
            / "reports"
            / "accepting-004.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["created_follow_up_task_ids"] == [follow_up.id]


def test_runner_persists_blocking_follow_up_and_blocks_current_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ship queue behavior")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "grooming":
            return StageReport(
                task_id=task.id,
                step=step,
                verdict="blocked",
                summary="blocked by separate prerequisite work",
                follow_up_tasks=[
                    {
                        "title": "Add missing prerequisite contract",
                        "rationale": "PM identified prerequisite contract work that must land first.",
                        "blocking": True,
                        "task_type": "bugfix",
                    }
                ],
            )
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "flagged"
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.status == "flagged"
    assert updated.runtime.last_outcome.kind == "blocked"
    follow_up = get_task(tmp_path, "T-0002")
    assert follow_up is not None
    assert follow_up.status == "queued"
    assert follow_up.created_from is not None
    assert follow_up.created_from.stage == "grooming"
    assert follow_up.created_from.blocking is True
    assert load_state(tmp_path).queue == [follow_up.id]


def test_run_next_task_executes_follow_up_created_by_acceptance_on_later_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    original = create_task(tmp_path, title="Ship feature behavior", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == original.id and task.pipeline_status == "accepting":
            return SubagentResult(
                ref=SubagentRef(
                    id="SA-accepting",
                    role=role,
                    engine="codex",
                    status="completed",
                    path="subagents/accepting",
                ),
                execution=CLIExecutionResult(
                    adapter="codex",
                    argv=("codex", "exec"),
                    cwd=tmp_path,
                    exit_code=0,
                    stdout=(
                        "VERDICT: PASS\n"
                        "SUMMARY: acceptance passed with separate follow-up\n"
                        "FILES_CHANGED:\n"
                        "- litehive/runner.py\n"
                        "TESTS_ADDED: 1\n"
                        "TESTS_PASSING: 1\n"
                        "WARNINGS:\n"
                        "FOLLOW_UP_TASKS:\n"
                        '[{"title":"Document follow-up feature behavior","rationale":"Acceptance found documentation work that should run next.","blocking":false}]'
                    ),
                    stderr="",
                ),
                transcript="",
                exit_code=0,
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    first = run_next_task(tmp_path)

    assert first.task is not None
    assert first.task.id == original.id
    assert first.result is not None
    assert first.result.final_status == "done"
    follow_up = require_task(tmp_path, "T-0002")
    assert load_state(tmp_path).queue == [follow_up.id]

    # Keep the follow-up on the focused non-commit path for this test.
    follow_up.git.auto_commit = False
    save_task(tmp_path, follow_up)

    second = run_next_task(tmp_path)

    assert second.task is not None
    assert second.task.id == follow_up.id
    assert second.result is not None
    assert second.result.final_status == "done"
    assert load_state(tmp_path).queue == []
    refreshed_follow_up = require_task(tmp_path, follow_up.id)
    assert refreshed_follow_up.status == "done"
    assert refreshed_follow_up.created_from is not None
    assert refreshed_follow_up.created_from.task_id == original.id


def test_unexpected_dirty_paths_computes_allowed_set_once(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Optimize commit dirty-path scan")

    dirty_entries = [
        f" M .litehive/tasks/{task.id}-{task.slug}/task.yaml",
        " M litehive/runtime.py",
        " M README.md",
        "?? docs/state-machine.md",
    ]

    allowed_paths = {
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
        PurePosixPath("litehive") / "runtime.py",
    }

    unexpected = _unexpected_dirty_paths(dirty_entries, allowed_paths)

    assert unexpected == ["README.md", "docs/state-machine.md"]


def test_unexpected_dirty_paths_ignores_unrelated_litehive_workspace_churn(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ignore workspace churn during commit")

    allowed_paths = {
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
        PurePosixPath("litehive") / "runtime.py",
    }

    dirty_entries = [
        f" M .litehive/tasks/{task.id}-{task.slug}/task.yaml",
        " M .litehive/tasks/T-0099-something-else/task.yaml",
        "?? .litehive/tasks/T-0099-something-else/journal.md",
        " M README.md",
    ]

    unexpected = _unexpected_dirty_paths(dirty_entries, allowed_paths)

    assert unexpected == ["README.md"]


def test_unexpected_dirty_paths_ignores_stray_tmpdir_workspace_cleanup(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ignore stray tmpdir cleanup")

    allowed_paths = {
        PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}",
        PurePosixPath("litehive") / "runtime.py",
    }

    dirty_entries = [
        ' D "\\"$tmpdir\\"/.litehive/config.yaml"',
        ' D "\\"$tmpdir\\"/.litehive/context.md"',
        ' D "\\"$tmpdir\\"/.litehive/state.yaml"',
        " M README.md",
    ]

    unexpected = _unexpected_dirty_paths(dirty_entries, allowed_paths)

    assert unexpected == ["README.md"]


def test_allowed_commit_paths_ignores_placeholder_file_entries(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Ignore placeholder changed files during commit")
    reports_dir = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    (reports_dir / "implementing-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "implementing",
                "verdict": "pass",
                "summary": "placeholder files changed",
                "files_changed": ["none", "litehive/runtime.py", " N/A ", "-"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    allowed_paths = _allowed_commit_paths(tmp_path, task)

    assert PurePosixPath("litehive/runtime.py") in allowed_paths
    assert PurePosixPath("none") not in allowed_paths
    assert PurePosixPath("N/A") not in allowed_paths
    assert PurePosixPath("-") not in allowed_paths


def test_live_session_progress_updates_runtime_heartbeat(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Live heartbeat update")
    task.pipeline_status = "testing"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-02T00:00:00+00:00",
        updated_at="2026-04-02T00:00:00+00:00",
    )
    ref = SubagentRef(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
    )
    task.subagents.append(ref)
    save_task(tmp_path, task)
    mark_subagent_started(tmp_path, task, ref)
    task_dir_path = task_dir(tmp_path, task) / "subagents" / "SA-0001-qa"
    task_dir_path.mkdir(parents=True, exist_ok=True)

    manager = SubagentManager(tmp_path)
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout="VERDICT: PASS\nSUMMARY: still running\n",
        stderr="",
        pid=12345,
    )
    manager._write_session_progress(task, task_dir_path, ref, "prompt", execution)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.updated_at != "2026-04-02T00:00:00+00:00"
    assert refreshed.runtime.current_stage.updated_at != "2026-04-02T00:00:00+00:00"
    assert refreshed.runtime.active_subagent is not None
    assert refreshed.runtime.active_subagent.updated_at != "2026-04-02T00:00:00+00:00"
    assert refreshed.runtime.active_subagent.pid == 12345
    assert refreshed.runtime.active_subagent.transcript_snippet


def test_commit_task_can_commit_only_selected_paths_with_other_unstaged_changes(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "litehive"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "litehive@example.com"], cwd=tmp_path, check=True)

    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("leave unstaged\n", encoding="utf-8")

    checkpoint = commit_task(tmp_path, "selected commit", paths=["tracked.txt"])

    assert checkpoint is not None
    status_lines = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "?? other.txt" in status_lines
    assert not any("tracked.txt" in line for line in status_lines)


def test_runner_preserves_retry_count_when_requeued_task_is_rejected_again(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=3))
    task = create_task(tmp_path, title="Review loop")
    task.status = "queued"
    task.pipeline_status = "accepting"
    task.runtime.retry_count = 1
    task.runtime.retry_limit = 3
    task.runtime.retry_source = "global"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        verdict = "reject" if step == "accepting" else "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}")

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.runtime.retry_count == 2
    assert task.runtime.retry_limit == 3
    assert task.runtime.retry_source == "global"

    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-review-loop"
            / "reports"
            / "accepting-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["retry_count"] == 2
    assert accepting_report["retry_limit"] == 3
    assert accepting_report["retry_decision"] == "retry"


def test_runner_requeues_implementing_rejection_without_sink_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Implementation rejection",
        acceptance_criteria=["Implement the requested change."],
    )
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        verdict = "reject" if step == "implementing" else "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}")

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=3)
    result = runner.run(task)

    assert result.final_status == "queued"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.last_outcome.kind is None
    assert refreshed.runtime.last_outcome.reason_code is None

    implementing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-implementation-rejection"
            / "reports"
            / "implementing-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert implementing_report["verdict"] == "reject"
    assert implementing_report["retry_decision"] == "retry"


def test_runner_infers_acceptance_criteria_from_task_context_after_grooming(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="First prerequisite")
    task = create_task(
        tmp_path,
        title="Implement feature",
        goal="Ship deterministic dispatch",
        depends_on=[prerequisite.id],
    )

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    finish_task_run_transition(tmp_path, task, result.final_status)
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.runtime.execution_status == "done"
    assert updated.acceptance_criteria == [
        "The delivered change achieves the stated goal: Ship deterministic dispatch.",
        f"The result aligns with the prerequisite task context needed from: {prerequisite.id}.",
        "Focused verification demonstrates the targeted behavior works as intended.",
    ]


def test_runner_blocks_large_task_without_inferable_acceptance_criteria_during_grooming(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="First prerequisite")
    task = create_task(tmp_path, title="Implement feature", depends_on=[prerequisite.id])

    def executor(task, step):  # type: ignore[no-untyped-def]
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "flagged"
    finish_task_run_transition(tmp_path, task, result.final_status)
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    assert flagged.status == "flagged"
    assert flagged.runtime.last_outcome.reason_code == "missing_acceptance_criteria"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0002-implement-feature"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert "Structured acceptance criteria are required before implementation for larger tasks." in report["summary"]


def test_runner_persists_grooming_generated_acceptance_criteria(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Implement feature", goal="Ship deterministic dispatch")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "grooming":
            transcript = (
                "VERDICT: PASS\n"
                "SUMMARY: grooming complete\n"
                "FILES_CHANGED:\n"
                "TESTS_ADDED: 0\n"
                "TESTS_PASSING: 0\n"
                "WARNINGS:\n"
                "ACCEPTANCE_CRITERIA:\n"
                "- The system auto-populates missing acceptance criteria from successful grooming output.\n"
                "- Tasks still block before implementation when grooming cannot define concrete criteria.\n"
            )
            return parse_stage_report_text(
                task_id=task.id,
                step="grooming",
                transcript=transcript,
                subagent_status="completed",
            )
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    finish_task_run_transition(tmp_path, task, result.final_status)
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.acceptance_criteria == [
        "The system auto-populates missing acceptance criteria from successful grooming output.",
        "Tasks still block before implementation when grooming cannot define concrete criteria.",
    ]


def test_runner_persists_grooming_generated_pm_sizing(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Implement feature", goal="Ship deterministic dispatch")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "grooming":
            transcript = (
                "VERDICT: PASS\n"
                "SUMMARY: grooming complete\n"
                "PM_COMPLEXITY: complex\n"
                "PLANNED_EFFORT: l\n"
                "FILES_CHANGED:\n"
                "TESTS_ADDED: 0\n"
                "TESTS_PASSING: 0\n"
                "WARNINGS:\n"
            )
            return parse_stage_report_text(
                task_id=task.id,
                step="grooming",
                transcript=transcript,
                subagent_status="completed",
            )
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "done"
    finish_task_run_transition(tmp_path, task, result.final_status)
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.pm_complexity == "complex"
    assert updated.planned_effort == "l"


def test_large_task_acceptance_criteria_requirement_heuristic() -> None:
    minimal = TaskRecord(id="T-0001", slug="small-task", title="Small task")
    assert task_requires_acceptance_criteria(minimal) is False
    assert implementation_entry_stage(minimal) == "implementing"
    assert reroute_stage_for_acceptance_criteria(
        minimal.model_copy(update={"pipeline_status": "testing"})
    ) == "testing"

    goal_only = TaskRecord(
        id="T-0002",
        slug="goal-task",
        title="Goal task",
        goal="Ship deterministic routing",
    )
    assert task_requires_acceptance_criteria(goal_only) is True
    assert implementation_entry_stage(goal_only) == "grooming"
    assert reroute_stage_for_acceptance_criteria(
        goal_only.model_copy(update={"pipeline_status": "testing"})
    ) == "grooming"

    dependency_scoped = TaskRecord(
        id="T-0003",
        slug="dependency-task",
        title="Dependency task",
        depends_on=["T-0001"],
    )
    assert task_requires_acceptance_criteria(dependency_scoped) is True

    priority_scoped = TaskRecord(
        id="T-0004",
        slug="priority-task",
        title="Priority task",
        priority="high",
    )
    assert task_requires_acceptance_criteria(priority_scoped) is True

    planned = TaskRecord(
        id="T-0005",
        slug="planned-task",
        title="Planned task",
        plan=["Inspect current flow", "Implement gate"],
    )
    assert task_requires_acceptance_criteria(planned) is True

    explicitly_scoped = planned.model_copy(
        update={"acceptance_criteria": ["The result ships deterministic routing."]}
    )
    assert task_requires_acceptance_criteria(explicitly_scoped) is True
    assert implementation_entry_stage(explicitly_scoped) == "implementing"
    assert reroute_stage_for_acceptance_criteria(
        explicitly_scoped.model_copy(update={"pipeline_status": "accepting"})
    ) == "accepting"


def test_runner_blocks_direct_implementing_stage_without_acceptance_criteria(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume feature", goal="Ship deterministic routing")
    task.plan = ["Inspect current flow", "Implement gate"]
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        raise AssertionError(f"executor should not run for {step}")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "flagged"
    finish_task_run_transition(tmp_path, task, result.final_status)
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    assert flagged.status == "flagged"
    assert flagged.runtime.last_outcome.stage == "implementing"
    assert flagged.runtime.last_outcome.reason_code == "missing_acceptance_criteria"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-resume-feature"
            / "reports"
            / "implementing-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert "Structured acceptance criteria are required before implementation for larger tasks." in report["summary"]


def test_runner_blocks_later_stage_without_acceptance_criteria(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume feature", goal="Ship deterministic routing")
    task.plan = ["Inspect current flow", "Implement gate"]
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        raise AssertionError(f"executor should not run for {step}")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "flagged"
    finish_task_run_transition(tmp_path, task, result.final_status)
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    assert flagged.status == "flagged"
    assert flagged.runtime.last_outcome.stage == "testing"
    assert flagged.runtime.last_outcome.reason_code == "missing_acceptance_criteria"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-resume-feature"
            / "reports"
            / "testing-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert "Structured acceptance criteria are required before implementation for larger tasks." in report["summary"]


def test_runner_cancels_task_with_explicit_reason(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Cancelled run")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            raise KeyboardInterrupt()
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "interrupted"
    finish_task_run_transition(tmp_path, task, result.final_status)
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "interrupted"
    assert load_state(tmp_path).queue == []
    assert task.runtime.execution_status == "interrupted"
    assert task.runtime.last_outcome.kind == "interrupted"
    assert task.runtime.last_outcome.stage == "testing"
    assert task.runtime.last_outcome.reason_code == "execution_interrupted"
    assert task.runtime.last_outcome.reason == "Execution interrupted during testing"
    assert task.runtime.interruption is not None
    assert task.runtime.interruption.source == "runner"
    assert task.runtime.interruption.stage == "testing"
    assert task.runtime.interruption.resume_stage == "testing"
    assert task.runtime.interruption.reason == "Execution interrupted during testing"
    assert task.runtime.current_stage.step == "testing"
    assert task.runtime.current_stage.status == "interrupted"
    journal = (
        tmp_path / ".litehive" / "tasks" / "T-0001-cancelled-run" / "journal.md"
    ).read_text(encoding="utf-8")
    assert "Interrupted runner execution while `testing` was running." in journal
    assert "Resume from `testing`." in journal
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-cancelled-run"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["retry_decision"] == "final"
    assert report["outcome"] == "interrupted"
    assert report["outcome_reason_code"] == "execution_interrupted"
    assert report["outcome_reason"] == "Execution interrupted during testing"


def test_runner_fails_task_when_stage_executor_crashes(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Executor crash")

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            raise RuntimeError("boom")
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "queued"
    finish_task_run_transition(tmp_path, task, result.final_status)
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert load_state(tmp_path).queue == [task.id]
    assert task.runtime.last_outcome.kind == "flagged"
    assert task.runtime.last_outcome.stage == "testing"
    assert task.runtime.last_outcome.reason_code == "stage_exception"
    assert task.runtime.last_outcome.reason == "testing failed with unhandled error: boom"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-executor-crash"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"] == ["boom"]
    assert report["retry_decision"] == "final"
    assert report["outcome"] == "flagged"
    assert report["outcome_reason_code"] == "stage_exception"
    assert report["outcome_reason"] == "testing failed with unhandled error: boom"

def test_run_next_task_uses_task_retry_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    task = create_task(tmp_path, title="Override retry limit", auto_commit=False)
    task.retry_policy.max_retries = 1
    save_task(tmp_path, task)
    attempts = {"testing": 0}

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.pipeline_status == "testing":
            attempts["testing"] += 1
            if attempts["testing"] == 1:
                transcript = "\n".join(
                    [
                        "VERDICT: FAIL",
                        "SUMMARY: tests failed once",
                        "FILES_CHANGED:",
                        "TESTS_ADDED: 0",
                        "TESTS_PASSING: 0",
                        "WARNINGS:",
                    ]
                )
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-testing-codex",
                        role=role,
                        engine=engine_name,
                        status="completed",
                        path="subagents/testing-codex",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "exec"),
                        cwd=tmp_path,
                        exit_code=0,
                        stdout=transcript,
                        stderr="",
                    ),
                    transcript=transcript,
                    exit_code=0,
                )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "queued"
    task = get_task(tmp_path, task.id)
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.runtime.retry_limit == 1
    assert task.runtime.retry_count == 1
    assert task.runtime.retry_source == "task"
    assert task.runtime.last_outcome.kind is None
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-override-retry-limit"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["retry_count"] == 1
    assert report["retry_limit"] == 1
    assert report["retry_source"] == "task"
    assert report["retry_decision"] == "retry"


def test_run_next_task_requeues_after_qa_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    task = create_task(tmp_path, title="Iterate until accepted", auto_commit=False)
    task.retry_policy.max_retries = 3
    save_task(tmp_path, task)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        transcript = "\n".join(
            [
                "VERDICT: PASS",
                f"SUMMARY: {task.pipeline_status} passed",
                "FILES_CHANGED:",
                "- app.txt",
                "TESTS_ADDED: 0",
                "TESTS_PASSING: 0",
                "WARNINGS:",
            ]
        )
        if task.pipeline_status == "testing":
            transcript = "\n".join(
                [
                    "VERDICT: FAIL",
                    "SUMMARY: testing needs another implementation pass",
                    "FILES_CHANGED:",
                    "TESTS_ADDED: 0",
                    "TESTS_PASSING: 0",
                    "WARNINGS:",
                ]
            )
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.pipeline_status}-codex",
                role=role,
                engine=engine_name,
                status="completed",
                path=f"subagents/{task.pipeline_status}-codex",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=0,
                stdout=transcript,
                stderr="",
            ),
            transcript=transcript,
            exit_code=0,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.task.id == task.id
    assert summary.result is not None
    assert summary.result.final_status == "queued"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.retry_count == 1
    assert refreshed.runtime.retry_limit == 3
    assert refreshed.runtime.retry_source == "task"
    assert refreshed.runtime.last_outcome.kind is None

    testing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-iterate-until-accepted"
            / "reports"
            / "testing-003.yaml"
        ).read_text(encoding="utf-8")
    )
    assert testing_report["verdict"] == "fail"
    assert testing_report["retry_count"] == 1
    assert testing_report["retry_limit"] == 3
    assert testing_report["retry_source"] == "task"
    assert testing_report["retry_decision"] == "retry"


def test_cli_run_end_to_end_requeues_after_qa_failure_then_commits_in_temp_git_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    task = create_task(tmp_path, title="QA harness task")
    task.retry_policy.max_retries = 2
    save_task(tmp_path, task)

    attempts = {"testing": 0}

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "implementing":
            (self.execution_root / "app.txt").write_text(
                f"iteration {attempts['testing'] + 1}\n",
                encoding="utf-8",
            )
        if current_task.pipeline_status == "testing":
            attempts["testing"] += 1
            if attempts["testing"] == 1:
                return _stage_subagent_result(
                    self.execution_root,
                    "testing",
                    role=role,
                    engine_name=engine_name,
                    verdict="FAIL",
                    summary="testing needs another implementation pass",
                    files_changed=[],
                    tests_added=0,
                    tests_passing=0,
                )
        return _stage_subagent_result(
            self.execution_root,
            current_task.pipeline_status,
            role=role,
            engine_name=engine_name,
            files_changed=["app.txt"],
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    assert _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False)) == 0
    first_output = capsys.readouterr().out
    assert "status: queued" in first_output
    assert "last_verdict: fail" in first_output
    assert "stage_outcomes: grooming=pass, implementing=pass, testing=fail" in first_output
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == initial_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"

    requeued = get_task(tmp_path, task.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "implementing"
    assert requeued.runtime.retry_count == 1
    assert requeued.runtime.retry_limit == 2

    assert _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False)) == 0
    second_output = capsys.readouterr().out
    assert "status: done" in second_output
    assert "last_verdict: pass" in second_output
    assert "stage_outcomes:" in second_output
    assert "grooming=pass" in second_output
    assert "implementing=pass" in second_output
    assert "testing=pass" in second_output
    assert "accepting=pass" in second_output
    assert "commit_to_git=pass" in second_output
    assert "commit:" in second_output

    finished = get_task(tmp_path, task.id)
    assert finished is not None
    assert finished.status == "done"
    assert finished.pipeline_status == "done"
    assert finished.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert finished.git.commit_sha != initial_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "iteration 2\n"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 qa-harness-task"
    )

    reports = task_dir(tmp_path, finished) / "reports"
    assert (reports / "grooming-001.yaml").exists()
    implementing_reports = sorted(reports.glob("implementing-*.yaml"))
    testing_reports = sorted(reports.glob("testing-*.yaml"))
    accepting_reports = sorted(reports.glob("accepting-*.yaml"))
    commit_reports = sorted(reports.glob("commit_to_git-*.yaml"))
    assert len(implementing_reports) == 2
    assert len(testing_reports) == 2
    assert len(accepting_reports) == 1
    assert len(commit_reports) == 1

    parsed_testing_reports = [
        yaml.safe_load(path.read_text(encoding="utf-8")) for path in testing_reports
    ]
    commit_report = yaml.safe_load(commit_reports[0].read_text(encoding="utf-8"))
    assert {report["verdict"] for report in parsed_testing_reports} == {"fail", "pass"}
    failed_testing = next(
        report for report in parsed_testing_reports if report["verdict"] == "fail"
    )
    assert failed_testing["retry_decision"] == "retry"
    assert commit_report["verdict"] == "pass"


def test_opencode_strips_provider_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {}

    class FakePopen:
        def __init__(self, cmd, cwd, env, stdout, stderr, text):  # type: ignore[no-untyped-def]
            calls["cmd"] = cmd
            calls["cwd"] = cwd
            calls["env"] = env
            self.pid = 4242
            self.returncode = 0

        def communicate(self):  # type: ignore[no-untyped-def]
            return ("ok", "")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/fake")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("OPENCODE_API_KEY", "secret2")

    engine = get_engine("opencode")
    result = engine.run("hello", tmp_path)

    assert result.returncode == 0
    assert calls["cwd"] == str(tmp_path)
    assert list(calls["cmd"]) == ["opencode", "run", "--format", "json", "--dir", str(tmp_path), "hello"]
    assert "OPENAI_API_KEY" not in calls["env"]
    assert "OPENCODE_API_KEY" not in calls["env"]


def test_sandbox_launcher_wraps_selected_engine_with_docker_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LitehiveConfig(
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
    )
    launcher = SandboxLauncher(tmp_path, config)
    creds_path = tmp_path / "google.json"
    creds_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)
    invocation = invocation.__class__(
        argv=invocation.argv,
        cwd=invocation.cwd,
        env={
            "OPENAI_API_KEY": "secret",
            "GOOGLE_APPLICATION_CREDENTIALS": str(creds_path),
            "ANTHROPIC_API_KEY": "should-not-leak",
        },
    )

    wrapped = launcher.wrap_invocation("codex", "codex", invocation)
    joined = " ".join(wrapped.argv)

    assert wrapped.cwd == tmp_path
    assert wrapped.argv[:5] == ("docker", "run", "--rm", "--init", "--pull=never")
    assert "--network none" in joined
    assert "--read-only" in joined
    assert f"src={tmp_path},dst=/workspace" in joined
    assert "src=/usr/bin/codex,dst=/litehive/bin/codex,readonly" in joined
    assert "src=/usr/bin/opencode" not in joined
    assert "--env OPENAI_API_KEY=secret" in joined
    assert "--env ANTHROPIC_API_KEY=should-not-leak" not in joined
    assert f"src={creds_path},dst=/run/credentials/google.json,readonly" in joined
    assert "--env GOOGLE_APPLICATION_CREDENTIALS=/run/credentials/google.json" in joined
    assert "/litehive/bin/codex exec --json --dangerously-bypass-approvals-and-sandbox --cd /workspace" in joined


def test_sandbox_launcher_applies_resource_limit_flags_from_profile_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LitehiveConfig(process_profile="rust")
    launcher = SandboxLauncher(tmp_path, config)

    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)

    wrapped = launcher.wrap_invocation("codex", "codex", invocation)
    joined = " ".join(wrapped.argv)

    assert wrapped.argv[:4] == ("docker", "run", "--rm", "--init")
    assert "--memory 8192m" in joined
    assert "--cpus 4" in joined
    assert "--pids-limit 512" in joined
    assert f"src={tmp_path},dst=/workspace" in joined


def test_sandbox_launcher_classifies_cpu_limit_events() -> None:
    launcher = SandboxLauncher(Path("/tmp/workspace"), LitehiveConfig(process_profile="rust"))

    event = launcher.classify_resource_limit_event(
        "codex",
        exit_code=1,
        stdout="",
        stderr="fatal error: CPU time limit exceeded by cgroup cpu controller",
    )

    assert event is not None
    assert event.resource == "cpu"
    assert event.reason == "CPU limit exceeded"
    assert event.observed_signal == "cpu_limit"
    assert event.cpu_count == 4.0


def test_gemini_build_invocation_includes_model_and_jsonl_flags(tmp_path: Path) -> None:
    invocation = get_engine("gemini").build_invocation(
        "ship it",
        tmp_path,
        model="gemini-2.5-pro",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "gemini",
        "-p",
        "ship it",
        "--output-format",
        "stream-json",
        "--yolo",
        "-m",
        "gemini-2.5-pro",
    ]


def test_copilot_build_invocation_includes_model_and_jsonl_flags(tmp_path: Path) -> None:
    invocation = get_engine("copilot").build_invocation(
        "ship it",
        tmp_path,
        model="gpt-5",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "copilot",
        "-p",
        "ship it",
        "--output-format",
        "json",
        "--allow-all-tools",
        "--autopilot",
        "--no-auto-update",
        "--add-dir",
        str(tmp_path),
        "--model",
        "gpt-5",
    ]


def test_engine_capabilities_report_availability_and_contract_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda binary: f"/usr/bin/{binary}")

    codex = get_engine("codex").detect_capabilities()
    opencode = get_engine("opencode").detect_capabilities()
    gemini = get_engine("gemini").detect_capabilities()
    copilot = get_engine("copilot").detect_capabilities()

    assert codex.available is True
    assert codex.supports_model_override is False
    assert codex.transcript_format == "jsonl"
    assert opencode.available is True
    assert opencode.supports_model_override is True
    assert opencode.strips_environment is True
    assert gemini.available is True
    assert gemini.supports_model_override is True
    assert gemini.transcript_format == "jsonl"
    assert copilot.available is True
    assert copilot.supports_model_override is True
    assert copilot.transcript_format == "jsonl"


def test_codex_build_invocation_includes_workspace_and_prompt(tmp_path: Path) -> None:
    invocation = get_engine("codex").build_invocation("ship it", tmp_path)

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(tmp_path),
        "--skip-git-repo-check",
        "ship it",
    ]


def test_codex_renders_jsonl_transcript_and_usage_observation(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec", "--json"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"019d5098-77ba-7dc1-8b89-d3bff176bdb1"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
                '{"type":"turn.completed","usage":{"input_tokens":15442,"cached_input_tokens":5504,"output_tokens":18}}',
            ]
        ),
        stderr="",
    )

    adapter = get_engine("codex")

    assert adapter.render_transcript(execution) == "OK"

    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "openai"
    assert observation.usage is not None
    assert observation.usage.used == 15460
    assert observation.usage.unit == "tokens"
    assert observation.metadata["input_tokens"] == 15442
    assert observation.metadata["cached_input_tokens"] == 5504
    assert observation.metadata["output_tokens"] == 18


def test_codex_renders_jsonl_error_payloads_and_extracts_limit_observation(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec", "--json"),
        cwd=tmp_path,
        exit_code=1,
        stdout="\n".join(
            [
                '{"type":"thread.started","thread_id":"019d5098-77ba-7dc1-8b89-d3bff176bdb1"}',
                '{"type":"turn.started"}',
                '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"message\\":\\"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 5:26 PM.\\"}}"}',
                '{"type":"turn.failed","error":{"message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"message\\":\\"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 5:26 PM.\\"}}"}}',
            ]
        ),
        stderr="",
    )

    adapter = get_engine("codex")
    transcript = adapter.render_transcript(execution)

    assert "usage limit" in transcript
    assert classify_execution_limit(transcript) == "usage limit reached"

    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "openai"
    assert observation.success is False
    assert observation.limit_reason == "usage limit reached"
    assert observation.usage is None
    assert observation.metadata["error_status"] == 429
    assert observation.metadata["error_type"] == "rate_limit_error"
    assert observation.metadata["retry_at_hint"] == "5:26 PM"
    assert observation.metadata["purchase_more_credits"] is True


def test_opencode_build_invocation_includes_dir_model_and_prompt(tmp_path: Path) -> None:
    invocation = get_engine("opencode").build_invocation(
        "ship it",
        tmp_path,
        model="zai-coding-plan/glm-5.1",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "opencode",
        "run",
        "--format",
        "json",
        "--dir",
        str(tmp_path),
        "--model",
        "zai-coding-plan/glm-5.1",
        "ship it",
    ]


def test_opencode_renders_json_transcript_and_usage_observation(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="opencode",
        argv=("opencode", "run", "--format", "json"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"step_start","timestamp":1,"sessionID":"ses_123","part":{"id":"prt_1","type":"step-start"}}',
                '{"type":"text","timestamp":2,"sessionID":"ses_123","part":{"id":"prt_2","type":"text","text":"OK"}}',
                '{"type":"step_finish","timestamp":3,"sessionID":"ses_123","part":{"id":"prt_3","type":"step-finish","reason":"stop","cost":0,"tokens":{"total":10971,"input":10509,"output":14,"reasoning":11,"cache":{"read":448,"write":0}}}}',
            ]
        ),
        stderr="",
    )

    adapter = get_engine("opencode")

    assert adapter.render_transcript(execution) == "OK"

    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "z.ai"
    assert observation.usage is not None
    assert observation.usage.used == 10971
    assert observation.usage.unit == "tokens"
    assert observation.metadata["input_tokens"] == 10509
    assert observation.metadata["output_tokens"] == 14
    assert observation.metadata["reasoning_tokens"] == 11
    assert observation.metadata["cache_read_tokens"] == 448
    assert observation.metadata["cache_write_tokens"] == 0
    assert observation.metadata["finish_reason"] == "stop"


def test_opencode_extract_usage_observation_reads_limit_error_payload(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="opencode",
        argv=("opencode", "run", "--format", "json"),
        cwd=tmp_path,
        exit_code=1,
        stdout=(
            '{"type":"error","timestamp":1,"sessionID":"ses_123",'
            '"error":{"name":"RateLimitError","data":{"message":"429 Too Many Requests: rate limit exceeded"}}}\n'
        ),
        stderr="",
    )

    adapter = get_engine("opencode")
    transcript = adapter.render_transcript(execution)

    assert "rate limit exceeded" in transcript
    assert classify_execution_limit(transcript) == "rate limit reached"

    observation = adapter.extract_usage_observation(execution)

    assert observation is not None
    assert observation.source == "provider"
    assert observation.provider == "z.ai"
    assert observation.success is False
    assert observation.limit_reason == "rate limit reached"
    assert observation.metadata["error_name"] == "RateLimitError"
    assert observation.metadata["error_message"] == "429 Too Many Requests: rate limit exceeded"


def test_gemini_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="gemini",
        argv=("gemini", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"init","session_id":"abc","model":"gemini-2.5-pro"}',
                '{"type":"message","role":"assistant","content":"VERDICT: PASS\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"SUMMARY: implemented Gemini adapter\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"FILES_CHANGED:\\n- litehive/engines.py\\n","delta":true}',
                '{"type":"message","role":"assistant","content":"TESTS_ADDED: 4\\nTESTS_PASSING: 4\\nWARNINGS:\\n","delta":true}',
                '{"type":"result","status":"success"}',
            ]
        ),
        stderr="",
    )

    engine = get_engine("gemini")

    assert engine.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = engine.parse_stage_report(
        task_id="T-0004",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "implemented Gemini adapter"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 4, "passing": 4}


def test_gemini_stage_report_uses_tool_error_when_no_assistant_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="gemini",
        argv=("gemini", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"tool_result","status":"error","error":{"message":"permission denied"}}',
        stderr="",
    )

    report = get_engine("gemini").parse_stage_report(
        task_id="T-0004",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "permission denied"
    assert report.verdict == "blocked"


def test_copilot_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.turn_start","data":{"turnId":"0"}}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"VERDICT: PASS\\n"},"ephemeral":true}',
                '{"type":"assistant.message","data":{"messageId":"m1","content":"VERDICT: PASS\\nSUMMARY: implemented Copilot adapter\\nFILES_CHANGED:\\n- litehive/engines.py\\nTESTS_ADDED: 2\\nTESTS_PASSING: 2\\nWARNINGS:\\n"}}',
                '{"type":"result","exitCode":0}',
            ]
        ),
        stderr="",
    )

    engine = get_engine("copilot")

    assert engine.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = engine.parse_stage_report(
        task_id="T-0005",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "implemented Copilot adapter"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 2, "passing": 2}


def test_copilot_stage_report_uses_json_error_when_no_assistant_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"error","data":{"message":"authentication required"}}',
        stderr="",
    )

    report = get_engine("copilot").parse_stage_report(
        task_id="T-0005",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "authentication required"
    assert report.verdict == "blocked"


def test_copilot_render_transcript_falls_back_to_message_deltas(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"assistant.turn_start","data":{"turnId":"0"}}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"VERDICT: PASS\\n"},"ephemeral":true}',
                '{"type":"assistant.message_delta","data":{"messageId":"m1","deltaContent":"SUMMARY: streamed only\\n"}}',
                '{"type":"assistant.turn_end","data":{"turnId":"0"}}',
            ]
        ),
        stderr="",
    )

    transcript = get_engine("copilot").render_transcript(execution)

    assert transcript == "VERDICT: PASS\nSUMMARY: streamed only"


def test_copilot_stage_report_uses_failed_tool_result_when_no_message(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="copilot",
        argv=("copilot", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout=(
            '{"type":"tool.execution_complete","data":{"toolName":"write","success":false,'
            '"result":{"content":"disk full"}}}'
        ),
        stderr="",
    )

    report = get_engine("copilot").parse_stage_report(
        task_id="T-0005",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "disk full"
    assert report.verdict == "blocked"


def test_execution_result_transcript_combines_stdout_and_stderr(tmp_path: Path) -> None:
    result = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=1,
        stdout="SUMMARY: failed\n",
        stderr="missing binary",
    )

    assert result.transcript == "SUMMARY: failed\n\n[stderr]\nmissing binary"


def test_parse_stage_report_text_extracts_shared_report_fields() -> None:
    report = parse_stage_report_text(
        task_id="T-0003",
        step="implementing",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: adapter contract added\n"
            "FILES_CHANGED:\n"
            "- litehive/engines.py\n"
            "- litehive/external_cli.py\n"
            "TESTS_ADDED: 3\n"
            "TESTS_PASSING: 8\n"
            "WARNINGS:\n"
            "- kept claude deferred\n"
        ),
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "adapter contract added"
    assert report.files_changed == ["litehive/engines.py", "litehive/external_cli.py"]
    assert report.tests == {"added": 3, "passing": 8}
    assert report.warnings == ["kept claude deferred"]


def test_parse_stage_report_text_extracts_follow_up_tasks() -> None:
    report = parse_stage_report_text(
        task_id="T-0003",
        step="accepting",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: acceptance complete\n"
            "FILES_CHANGED:\n"
            "- litehive/runner.py\n"
            "TESTS_ADDED: 2\n"
            "TESTS_PASSING: 2\n"
            "WARNINGS:\n"
            "FOLLOW_UP_TASKS:\n"
            '[{"title":"Tighten queue recovery coverage","rationale":"Acceptance found an uncovered recovery path.","blocking":false,"task_type":"bugfix","acceptance_criteria":["Recovery keeps queued follow-ups durable."]}]'
        ),
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert len(report.follow_up_tasks) == 1
    assert report.follow_up_tasks[0].title == "Tighten queue recovery coverage"
    assert report.follow_up_tasks[0].rationale == "Acceptance found an uncovered recovery path."
    assert report.follow_up_tasks[0].blocking is False
    assert report.follow_up_tasks[0].task_type == "bugfix"
    assert report.follow_up_tasks[0].acceptance_criteria == [
        "Recovery keeps queued follow-ups durable."
    ]


def test_parse_stage_report_text_accepts_inline_empty_follow_up_tasks_array() -> None:
    report = parse_stage_report_text(
        task_id="T-0003",
        step="accepting",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: acceptance complete\n"
            "FOLLOW_UP_TASKS: []"
        ),
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.follow_up_tasks == []
    assert report.warnings == []


def test_parse_stage_report_text_accepts_block_empty_follow_up_tasks_array() -> None:
    report = parse_stage_report_text(
        task_id="T-0003",
        step="accepting",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: acceptance complete\n"
            "FOLLOW_UP_TASKS:\n"
            "[]\n"
            "WARNINGS:\n"
        ),
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.follow_up_tasks == []
    assert report.warnings == []


def test_parse_stage_report_text_extracts_inline_follow_up_tasks_array() -> None:
    report = parse_stage_report_text(
        task_id="T-0003",
        step="accepting",
        transcript=(
            "VERDICT: PASS\n"
            "SUMMARY: acceptance complete\n"
            'FOLLOW_UP_TASKS: [{"title":"Tighten queue recovery coverage","rationale":"Acceptance found an uncovered recovery path.","blocking":false}]'
        ),
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert len(report.follow_up_tasks) == 1
    assert report.follow_up_tasks[0].title == "Tighten queue recovery coverage"
    assert report.follow_up_tasks[0].rationale == "Acceptance found an uncovered recovery path."
    assert report.follow_up_tasks[0].blocking is False


def test_stage_report_from_subagent_uses_adapter_execution_transcript(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Adapter task")
    result = SubagentResult(
        ref=SubagentRef(
            id="SA-0001",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/SA-0001-swe",
        ),
        execution=CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout="VERDICT: PASS\nSUMMARY: execution transcript parsed",
            stderr="",
        ),
        transcript="ignored fallback transcript",
        exit_code=0,
    )

    report = stage_report_from_subagent(task, "implementing", result)

    assert report.summary == "execution transcript parsed"
    assert report.verdict == "pass"


def test_stage_prompt_includes_shared_process_and_profile_overlay(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")
    prompt = stage_prompt(
        task,
        "testing",
        workspace_context="## Project\n- Purpose: validate overlays",
        process_profile="codehive",
    )

    assert "Process profile: Codehive-style" in prompt
    assert (
        "Shared stages: grooming -> implementing -> testing -> accepting -> commit_to_git."
        in prompt
    )
    assert (
        "Routing model: manager-owned deterministic routing, retries, and escalation stay in local code rather than prompts."
        in prompt
    )
    assert "the orchestrator is the manager; subagents execute but do not choose routing." in prompt
    assert (
        "Combine the generic base prompt with the selected project overlay instead of replacing the base."
        in prompt
    )
    assert "Verification should be independent enough to catch behavioral regressions" in prompt
    assert "default to regression-first or test-first implementation" in prompt
    assert "accepted tasks commit by default at commit_to_git" in prompt


def test_stage_prompt_surfaces_acceptance_gate_for_large_task_without_inferable_criteria(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="First prerequisite")
    task = create_task(tmp_path, title="Profiled task", depends_on=[prerequisite.id])

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Stage owner: planner" in prompt
    assert "You are the planner, a PM-style role representing the user's and product's point of view." in prompt
    assert "Acceptance gate:" in prompt
    assert "Structured acceptance criteria are required before implementation for larger tasks." in prompt
    assert "As the planner for grooming, provide an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets before passing grooming." in prompt
    assert "ACCEPTANCE_CRITERIA:" in prompt
    assert "If the context is still insufficient, return `VERDICT: BLOCKED`" in prompt


def test_stage_prompt_allows_grooming_to_pass_with_inferred_acceptance_criteria(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")
    task.plan = ["Inspect current flow", "Implement gate"]

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Stage owner: planner" in prompt
    assert "Acceptance gate:" in prompt
    assert "As the planner for grooming, either provide explicit `ACCEPTANCE_CRITERIA:` bullets or let the runner persist the inferred version by returning `VERDICT: PASS`." in prompt
    assert "If the current task context is not sufficient after all, return `VERDICT: BLOCKED` instead of passing grooming without criteria." in prompt
    assert "you may add an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets" in prompt
    assert "the current task context is sufficient to infer them" in prompt
    assert "You may return `VERDICT: PASS` without restating them" in prompt
    assert "Return `VERDICT: BLOCKED` only if the inferred criteria are incomplete or incorrect" in prompt
    assert "Structured acceptance criteria are required before implementation for larger tasks." not in prompt


def test_stage_prompt_shows_inferred_acceptance_criteria_when_context_is_sufficient(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Profiled task", goal="Ship deterministic routing")
    task.plan = ["Inspect current flow", "Implement gate"]

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Inferred acceptance criteria available from current task context:" in prompt
    assert "The delivered change achieves the stated goal: Ship deterministic routing." in prompt
    assert "Focused verification demonstrates the targeted behavior works as intended." in prompt


def test_stage_prompt_distinguishes_accepting_reviewer_role(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Review final outcome",
        acceptance_criteria=["The user-visible outcome works end to end."],
    )

    prompt = stage_prompt(task, "accepting", workspace_context="")

    assert "Stage owner: reviewer" in prompt
    assert "You are the reviewer, a PM-style role representing the user's and product's point of view." in prompt
    assert "Validate the strict end-user outcome, look for regressions or missing evidence, and make a final done versus not-done judgment." in prompt


def test_stage_prompt_uses_recovery_role_when_requested(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover final stage")
    task.status = "flagged"
    task.pipeline_status = "implementing"
    task.runtime.last_outcome.kind = "flagged"
    task.runtime.last_outcome.reason_code = "stage_exception"
    task.runtime.last_outcome.reason = "implementing failed with unhandled error: boom"
    save_task(tmp_path, task)

    prompt = stage_prompt(task, "implementing", workspace_context="", role_name="recovery")

    assert "Stage owner: recovery" in prompt
    assert "You are the recovery agent responsible for diagnosing why this task stopped making progress and restoring a runnable path." in prompt
    assert "Make the smallest effective fix needed so the task can resume the current stage and finish cleanly." in prompt


def test_stage_prompt_includes_project_startup_guidance_for_role(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recovery-heavy task")
    config = LitehiveConfig(
        agent_startup_guidance={
            "all": ["Start from the latest task-local artifacts before broad repo reads."],
            "qa": ["Check the latest implementing report and wrapper logs before rerunning tests."],
        }
    )

    prompt = stage_prompt(task, "testing", workspace_context="", config=config)

    assert "Project startup guidance:" in prompt
    assert "Start from the latest task-local artifacts before broad repo reads." in prompt
    assert "Check the latest implementing report and wrapper logs before rerunning tests." in prompt


def test_stage_prompt_includes_task_type_and_plan(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Review adapter update", task_type="review", mode="tasks")

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "Task type: review" in prompt
    assert "Plan:" in prompt
    assert "Inspect the relevant change or workflow surface." in prompt
    assert "Task template:" in prompt
    assert "Prioritize correctness, regressions, and missing verification over style observations." in prompt
    assert "Template sections to fill or verify:" in prompt
    assert "Findings: record actionable issues with severity and supporting evidence." in prompt


def test_stage_prompt_includes_pm_sizing_guidance_for_grooming(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Estimate task", pm_complexity="moderate", planned_effort="m")

    prompt = stage_prompt(task, "grooming", workspace_context="")

    assert "PM sizing:" in prompt
    assert "Current PM complexity: moderate" in prompt
    assert "Current planned effort: m" in prompt
    assert "Use `PM_COMPLEXITY: simple|moderate|complex`." in prompt
    assert "Use `PLANNED_EFFORT: xs|s|m|l|xl`." in prompt


def test_load_config_normalizes_agent_startup_guidance_keys(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (tmp_path / ".litehive" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_startup_guidance": {
                    "QA": [
                        "Check the latest report first.",
                        "  ",
                    ],
                    "all": ["Use task-local artifacts before broad repo reads."],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.agent_startup_guidance == {
        "qa": ["Check the latest report first."],
        "all": ["Use task-local artifacts before broad repo reads."],
    }


def test_stage_prompt_requires_real_lifecycle_verification_for_workflow_testing_tasks(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Enforce workflow verification",
        goal="Prove workflow/control-plane behavior through the real CLI lifecycle",
        acceptance_criteria=[
            "commit_to_git succeeds and records the final checkpoint commit",
            "An interrupted task resumes from the correct stage in a real workspace run",
            "run-all behavior is proven through the wrapper or real CLI execution",
        ],
    )

    prompt = stage_prompt(task, "testing", workspace_context="")

    assert "This task touches workflow or control-plane behavior" in prompt
    assert "Reject workflow changes that are only covered by isolated unit tests" in prompt
    assert "require proof that `commit_to_git` succeeded and recorded the final checkpoint commit" in prompt
    assert "CLI evidence also shows Litehive recorded clean completion" in prompt
    assert "require proof that an interrupted task resumes from the correct stage in a real workspace run" in prompt
    assert "require proof through the wrapper or real CLI execution rather than direct helper-function tests" in prompt


def test_stage_prompt_requires_acceptance_to_match_workflow_qa_evidence(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Accept workflow verification evidence",
        goal="Only accept workflow claims that real Litehive runs demonstrated",
        acceptance_criteria=[
            "QA rejects helper-only lifecycle evidence",
            "Final task commit exists after commit_to_git",
        ],
    )

    prompt = stage_prompt(task, "accepting", workspace_context="")

    assert "Reject the task if QA did not demonstrate the real lifecycle behavior the task claims" in prompt
    assert "Reject the task if the implementation promise is broader than the QA evidence" in prompt
    assert "require proof that `commit_to_git` succeeded and recorded the final checkpoint commit" in prompt
    assert "CLI evidence also shows Litehive recorded clean completion" in prompt


def test_stage_prompt_requires_real_lifecycle_verification_for_normalized_workflow_terms(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Enforce control plane completion reliability",
        goal="Prove workflow/control plane behavior through the real CLI lifecycle",
        acceptance_criteria=[
            "Completion reliability is only proven after commit to git records the final checkpoint commit",
            "Interrupted tasks remain resumable from the correct stage in a real workspace run",
            "Run all behavior is proven through the wrapper or real CLI execution",
        ],
    )

    prompt = stage_prompt(task, "testing", workspace_context="")

    assert "This task touches workflow or control-plane behavior" in prompt
    assert "require proof that `commit_to_git` succeeded and recorded the final checkpoint commit" in prompt
    assert "CLI evidence also shows Litehive recorded clean completion" in prompt
    assert "require proof that an interrupted task resumes from the correct stage in a real workspace run" in prompt
    assert "require proof through the wrapper or real CLI execution rather than direct helper-function tests" in prompt


def test_stage_prompt_detects_normalized_control_plane_commit_and_resume_terms(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Control plane clean completion",
        goal="Keep interrupted tasks resumable and verify commit to git behavior through the CLI",
        acceptance_criteria=["Run all wrapper proves the pool behavior"],
    )

    prompt = stage_prompt(task, "testing", workspace_context="")

    assert "This task touches workflow or control-plane behavior" in prompt
    assert "require proof that `commit_to_git` succeeded and recorded the final checkpoint commit" in prompt
    assert "CLI evidence also shows Litehive recorded clean completion" in prompt
    assert "require proof that an interrupted task resumes from the correct stage in a real workspace run" in prompt
    assert "require proof through the wrapper or real CLI execution rather than direct helper-function tests" in prompt


def test_update_command_seeds_task_brief_when_switching_to_tasks_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Review queue behavior")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type="review",
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode="tasks",
            auto_commit=None,
        )
    )
    capsys.readouterr()

    assert exit_code == 0
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")
    assert "# T-0001 Review queue behavior" in brief
    assert "- Task type: review" in brief
    assert "## Template Guidance" in brief
    assert "## Intake Notes" in brief
    assert "### Findings" in brief
    assert "_TBD_" in brief


def test_run_next_task_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = run_next_task(tmp_path)

    assert summary.task is None
    assert summary.result is None


def test_drain_task_pool_no_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    summary = drain_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "queue_exhausted"


def test_drain_task_pool_drains_dynamic_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == first.id and get_task(tmp_path, "T-0002") is None:
            create_task(tmp_path, title="Second task", auto_commit=False)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        "T-0001",
        "T-0002",
    ]
    assert summary.stop_reason == "queue_exhausted"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == []
    second = get_task(tmp_path, "T-0002")
    assert second is not None
    assert second.status == "done"


def test_run_next_task_falls_back_to_next_engine_on_execution_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback task", engine="codex", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if engine_name == "codex":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="rate limit exceeded",
                    stderr="",
                ),
                transcript="rate limit exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="rate limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `opencode` after rate limit reached."
        in report["warnings"]
    )


def test_run_next_task_flags_when_limit_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.pipeline_status}-{engine_name}",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{task.pipeline_status}-{engine_name}",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="quota exceeded",
                stderr="",
            ),
            transcript="quota exceeded",
            exit_code=1,
            failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-exhausted-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert report["summary"] == "grooming blocked after exhausting engine fallbacks: quota exceeded"


def test_drain_task_pool_stops_by_default_when_limit_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="quota exceeded",
                    stderr="",
                ),
                transcript="quota exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_fallbacks_exhausted"
    state = load_state(tmp_path)
    assert state.queue == ["T-0002"]
    assert state.pool_stop_reason == "execution_limit_fallbacks_exhausted"
    journal = (
        tmp_path / ".litehive" / "tasks" / "T-0001-exhausted-fallback-task" / "journal.md"
    ).read_text(encoding="utf-8")
    assert "Pool stopped: execution_limit_fallbacks_exhausted." in journal
    assert "grooming blocked after exhausting engine fallbacks: quota exceeded" in journal


def test_drain_task_pool_rereads_queue_order_between_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    third = create_task(tmp_path, title="Third task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == first.id:
            move_queued_task(tmp_path, third.id, 1)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        first.id,
        third.id,
        second.id,
    ]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_drain_task_pool_allows_future_queue_mutation_during_active_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    started = threading.Event()
    resume = threading.Event()
    completed: dict[str, object] = {}
    failures: list[BaseException] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == first.id:
            started.set()
            assert resume.wait(timeout=5)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    def run_pool() -> None:
        try:
            completed["summary"] = drain_task_pool(tmp_path)
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            failures.append(exc)
            resume.set()

    thread = threading.Thread(target=run_pool)
    thread.start()
    assert started.wait(timeout=5)

    third = create_task(tmp_path, title="Third task", auto_commit=False)
    move_queued_task(tmp_path, third.id, 1)
    updated = update_task_metadata(
        tmp_path,
        third.id,
        priority="high",
        goal="Run before the older pending work once the active task finishes.",
    )

    with pytest.raises(
        WorkspaceConflictError,
        match="runner is actively using task state that cannot be changed concurrently",
    ):
        update_task_metadata(tmp_path, first.id, priority="high")

    with pytest.raises(
        WorkspaceConflictError,
        match="workspace is already being mutated by another runner",
    ):
        dequeue_next_task_selection(tmp_path)

    resume.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []

    assert "summary" in completed
    summary = completed["summary"]
    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        first.id,
        third.id,
        second.id,
    ]
    assert summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, third.id)
    assert refreshed is not None
    assert refreshed.priority == "high"
    assert refreshed.goal == "Run before the older pending work once the active task finishes."
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []
    assert updated.id == third.id


def test_drain_task_pool_picks_up_requeued_task_between_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    requeued = create_task(tmp_path, title="Retried task", auto_commit=False)
    requeued.status = "flagged"
    requeued.pipeline_status = "testing"
    save_task(tmp_path, requeued)

    state = load_state(tmp_path)
    state.queue = [first.id]
    save_state(tmp_path, state)
    requeued_once = False

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        nonlocal requeued_once
        if task.id == first.id and not requeued_once:
            requeue_task(tmp_path, requeued.id)
            requeued_once = True
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [
        first.id,
        requeued.id,
    ]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []
    refreshed = get_task(tmp_path, requeued.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"


def test_drain_task_pool_honors_stop_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = drain_task_pool(tmp_path, stop_when=lambda executions: len(executions) >= 1)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "stop_condition_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_drain_task_pool_restores_preselected_active_task_when_stop_condition_hits(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == first.id
    assert load_state(tmp_path).queue == [second.id]

    summary = drain_task_pool(tmp_path, stop_when=lambda executions: True)

    assert summary.executions == []
    assert summary.stop_reason == "stop_condition_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [second.id, first.id]


def test_drain_task_pool_pauses_for_human_checkpoint_before_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    checkpointed = create_task(
        tmp_path,
        title="Needs review before acceptance",
        human_checkpoints=["before_acceptance"],
        auto_commit=False,
    )
    queued = create_task(tmp_path, title="Waiting behind review", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [checkpointed.id]
    assert summary.stop_reason == "human_checkpoint_before_acceptance"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "accepting"
    assert refreshed.runtime.execution_status == "paused"
    assert load_state(tmp_path).queue == [checkpointed.id, queued.id]

    resume_summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in resume_summary.executions if execution.task is not None
    ] == [checkpointed.id, queued.id]
    assert resume_summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"


def test_drain_task_pool_pauses_for_human_checkpoint_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    checkpointed = create_task(
        tmp_path,
        title="Needs review before commit",
        human_checkpoints=["before_commit"],
        auto_commit=False,
    )
    queued = create_task(tmp_path, title="Waiting behind commit review", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [checkpointed.id]
    assert summary.stop_reason == "human_checkpoint_before_commit"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "paused"
    assert load_state(tmp_path).queue == [checkpointed.id, queued.id]

    resume_summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in resume_summary.executions if execution.task is not None
    ] == [checkpointed.id, queued.id]
    assert resume_summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, checkpointed.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"


def test_restore_untouched_active_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    set_active_task(tmp_path, first.id)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        restore_untouched_active_task(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id == first.id
    assert restored.queue == [second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"


def test_restore_untouched_active_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, interrupted)

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, interrupted),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        restore_untouched_active_task(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id == interrupted.id
    assert restored.queue == [queued.id]
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.current_stage.status == "running"
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted run recovered. Resume from `testing`." not in journal


def test_restore_untouched_active_task_requeues_interrupted_commit_stage_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    stranded = create_task(tmp_path, title="Halted commit stage", auto_commit=False)

    stranded.status = "in_progress"
    stranded.pipeline_status = "commit_to_git"
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    restore_untouched_active_task(tmp_path)

    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.run_started_at is None
    assert refreshed.runtime.current_stage.step == "commit_to_git"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.source == "runner"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id, stranded.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal


def test_restore_untouched_active_task_requeues_interrupted_non_commit_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    restore_untouched_active_task(tmp_path)

    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.run_started_at is None
    assert refreshed.runtime.current_stage.step == "testing"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.source == "runner"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id, interrupted.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `testing` was running." in journal
    assert "Resume from `testing`." in journal


def test_repair_workspace_state_requeues_system_interrupted_task_but_not_cli_stopped_task(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    system_task = create_task(tmp_path, title="System halted task", auto_commit=False)
    parked_task = create_task(tmp_path, title="CLI stopped task", auto_commit=False)

    system_task.status = "interrupted"
    system_task.pipeline_status = "testing"
    system_task.runtime.execution_status = "interrupted"
    system_task.runtime.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="runner died",
        summary="Interrupted run recovered. Resume from `testing`.",
    )
    save_task(tmp_path, system_task)
    save_task_runtime(tmp_path, system_task)

    parked_task.status = "interrupted"
    parked_task.pipeline_status = "testing"
    parked_task.runtime.execution_status = "interrupted"
    parked_task.runtime.interruption = RuntimeInterruptionState(
        source="runner",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="Task stopped via CLI",
        summary="Execution interrupted via `litehive stop`. Resume from `testing`.",
    )
    save_task(tmp_path, parked_task)
    save_task_runtime(tmp_path, parked_task)

    repair_workspace_state(tmp_path)

    state = load_state(tmp_path)
    assert system_task.id in state.queue
    assert parked_task.id not in state.queue


def test_repair_workspace_state_restores_flagged_task_into_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Flagged reprocess task", auto_commit=False)
    flagged.status = "flagged"
    flagged.pipeline_status = "testing"
    flagged.runtime.execution_status = "flagged"
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)

    repair_workspace_state(tmp_path)

    state = load_state(tmp_path)
    assert flagged.id in state.queue


def test_dequeue_next_task_selection_recovers_flagged_task_to_implementation_entry_stage(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Recover flagged task", auto_commit=False)
    flagged.status = "flagged"
    flagged.pipeline_status = "testing"
    flagged.runtime.execution_status = "flagged"
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)

    state = load_state(tmp_path)
    state.queue = [flagged.id]
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == flagged.id
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.execution_status == "idle"


def test_restore_untouched_active_task_requeues_stranded_done_commit_without_checkpoint(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    stranded = create_task(tmp_path, title="Halted save stage", auto_commit=False)

    stranded.status = "done"
    stranded.pipeline_status = "done"
    stranded.git.checkpoint_attempts = 1
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    restore_untouched_active_task(tmp_path)

    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.git.commit_sha is None
    assert refreshed.runtime.execution_status == "interrupted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id, stranded.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal


def test_peek_next_task_selection_auto_recovers_stale_runner_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.run_started_at = "2026-04-01T00:00:00+00:00"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    interrupted.subagents.append(
        SubagentRef(
            id="SA-0001",
            role="qa",
            engine="codex",
            status="running",
            path="subagents/SA-0001-qa",
        )
    )
    interrupted.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
        pid=4242,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:00:10+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)
    subagent_base = task_dir(tmp_path, interrupted) / "subagents" / "SA-0001-qa"
    subagent_base.mkdir(parents=True, exist_ok=False)
    (subagent_base / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "status": "running",
                "summary": "halfway through targeted testing",
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
                "warnings": [],
                "resource_limit_event": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": 999999, "started_at": "2026-04-01T00:00:00+00:00"}, sort_keys=False),
        encoding="utf-8",
    )

    selection = peek_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == queued.id
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.status == "interrupted"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.subagents[-1].status == "interrupted"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [queued.id, interrupted.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted subagent execution while `testing` was running." in journal
    assert "Resume from `testing`." in journal


def test_dequeue_next_task_selection_rolls_back_claim_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    _fail_atomic_write_on_path(
        monkeypatch,
        tmp_path / ".litehive" / "state.yaml",
        message="state write failed",
    )

    with pytest.raises(OSError, match="state write failed"):
        dequeue_next_task_selection(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [first.id, second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "queued"


def test_dequeue_next_task_selection_rolls_back_claim_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, first),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        dequeue_next_task_selection(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [first.id, second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.execution_status == "idle"


def test_dequeue_next_task_selection_rolls_back_claim_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, first),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        dequeue_next_task_selection(tmp_path)

    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [first.id, second.id]
    refreshed = get_task(tmp_path, first.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.execution_status == "idle"


def test_finish_task_run_transition_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish task", auto_commit=False)
    set_active_task(tmp_path, task.id)

    task.runtime.execution_status = "running"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path=".litehive/tasks/T-0001-finish-task/subagents/SA-0001",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
    )
    save_task_runtime(tmp_path, task)
    task.status = "done"
    task.pipeline_status = "done"

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        finish_task_run_transition(tmp_path, task, "done")

    restored = load_state(tmp_path)
    assert restored.active_task_id == task.id
    assert restored.queue == []
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.active_subagent is not None


def test_finish_task_run_transition_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish task", auto_commit=False)
    set_active_task(tmp_path, task.id)

    task.runtime.execution_status = "running"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path=".litehive/tasks/T-0001-finish-task/subagents/SA-0001",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
    )
    save_task_runtime(tmp_path, task)
    task.status = "done"
    task.pipeline_status = "done"

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        finish_task_run_transition(tmp_path, task, "done")

    restored = load_state(tmp_path)
    assert restored.active_task_id == task.id
    assert restored.queue == []
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.active_subagent is not None


def test_finish_task_run_transition_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish task", auto_commit=False)
    set_active_task(tmp_path, task.id)

    task.runtime.execution_status = "running"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path=".litehive/tasks/T-0001-finish-task/subagents/SA-0001",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
    )
    save_task_runtime(tmp_path, task)
    task.status = "done"
    task.pipeline_status = "done"

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        finish_task_run_transition(tmp_path, task, "done")

    restored = load_state(tmp_path)
    assert restored.active_task_id == task.id
    assert restored.queue == []
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.active_subagent is not None


def test_drain_task_pool_stops_after_max_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = drain_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(max_tasks=1))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "max_tasks_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_drain_task_pool_stops_on_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Failing task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="quota exceeded",
                    stderr="",
                ),
                transcript="quota exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_failure=True)
    )

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "failure_detected"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_drain_task_pool_stops_on_execution_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="budget exceeded",
                    stderr="",
                ),
                transcript="budget exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="budget limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_execution_limit=True)
    )

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "execution_limit_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_drain_task_pool_stops_on_quota_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second limit task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Third task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id in {"T-0001", "T-0002"}:
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="quota exceeded",
                    stderr="",
                ),
                transcript="quota exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(quota_threshold=2))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001", "T-0002"]
    assert summary.stop_reason == "quota_threshold_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0003", "T-0001"]


def test_drain_task_pool_stops_on_budget_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Budget task", engine="codex", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if task.id == "T-0001":
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{task.pipeline_status}-{engine_name}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{task.pipeline_status}-{engine_name}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="budget exceeded",
                    stderr="",
                ),
                transcript="budget exceeded",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="budget limit reached"),
            )
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(budget_threshold=1))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "budget_threshold_reached"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]


def test_drain_task_pool_stops_on_dirty_git_state(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    summary = drain_task_pool(
        tmp_path, stop_conditions=TaskPoolStopConditions(stop_on_dirty_git=True)
    )

    assert summary.executions == []
    assert summary.stop_reason == "dirty_git_state"


def test_drain_task_pool_stops_on_pool_usage_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = drain_task_pool(tmp_path, stop_conditions=TaskPoolStopConditions(pool_usage_cap=4))

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "pool_usage_cap_reached"
    state = load_state(tmp_path)
    assert state.queue == ["T-0002"]


def test_drain_task_pool_stops_on_pool_cost_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    create_task(tmp_path, title="First task", engine="claude", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = drain_task_pool(
        tmp_path,
        stop_conditions=TaskPoolStopConditions(
            pool_cost_cap=12,
            engine_costs={"claude": 3, "codex": 1},
        ),
    )

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == ["T-0001"]
    assert summary.stop_reason == "pool_cost_cap_reached"
    state = load_state(tmp_path)
    assert state.queue == ["T-0002"]


def test_run_next_task_skips_engine_when_usage_cap_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback task", engine="codex", auto_commit=False)
    calls: list[str] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        calls.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task(
        tmp_path,
        require_task(tmp_path, "T-0001"),
        budget_ledger=EngineBudgetLedger(
            engine_usage_caps={"codex": 0},
            engine_costs={"codex": 1, "opencode": 1},
        ),
    )

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls
    assert calls[0] == "opencode"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"


def test_run_next_task_blocks_when_claude_budget_is_exhausted_before_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            claude_enabled=True,
            engine_budget_caps={"claude": 2},
            engine_costs={"claude": 3},
        ),
    )
    create_task(tmp_path, title="Claude task", engine="claude", auto_commit=False)

    def fail_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("SubagentManager.run should not be called when claude is over budget")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fail_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-claude-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["verdict"] == "blocked"
    assert "engine budget cap reached for `claude`" in report["summary"]
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_resolve_next_task_prefers_active_without_mutating_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")

    set_active_task(tmp_path, first.id)
    state_before = load_state(tmp_path)

    task = resolve_next_task(tmp_path)
    state_after = load_state(tmp_path)

    assert task is not None
    assert task.id == first.id
    assert state_before == state_after
    assert second.id in state_after.queue


def test_resolve_next_task_clears_stale_active_and_returns_queued_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending task")
    # Simulate a stale active_task_id by writing state directly (T-9999 does not exist on disk)
    state = load_state(tmp_path)
    state.active_task_id = "T-9999"
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == queued.id
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id]


def test_resolve_next_task_skips_ineligible_active_and_queue_entries(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Flagged active task")
    queued = create_task(tmp_path, title="Real pending task")
    completed = create_task(tmp_path, title="Completed prior task")

    active.status = "flagged"
    save_task(tmp_path, active)
    completed.status = "done"
    completed.pipeline_status = "done"
    save_task(tmp_path, completed)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [completed.id, queued.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == active.id


def test_resolve_next_task_prefers_ready_prerequisite_over_earlier_blocked_dependent(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked dependent")
    unrelated = create_task(tmp_path, title="Unrelated ready task")
    prerequisite = create_task(tmp_path, title="Ready prerequisite")

    blocked.depends_on = [prerequisite.id]
    save_task(tmp_path, blocked)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == unrelated.id


def test_resolve_next_task_fifo_prefers_earliest_ready_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="fifo"))
    first = create_task(tmp_path, title="First ready task")
    second = create_task(tmp_path, title="Second ready task")

    second.priority = "high"
    save_task(tmp_path, second)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == first.id


def test_resolve_next_task_priority_first_prefers_high_priority_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="priority_first"))
    first = create_task(tmp_path, title="First ready task")
    second = create_task(tmp_path, title="Second ready task")

    first.priority = "low"
    second.priority = "high"
    save_task(tmp_path, first)
    save_task(tmp_path, second)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == second.id


def test_resolve_next_task_priority_first_still_resumes_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="priority_first"))
    interrupted = create_task(tmp_path, title="Halted task")
    queued = create_task(tmp_path, title="New high priority task")

    queued.priority = "high"
    save_task(tmp_path, queued)
    set_active_task(tmp_path, interrupted.id)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == interrupted.id


def test_resolve_next_task_fifo_prefers_interrupted_queued_task_before_new_work(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="fifo"))
    new_task = create_task(tmp_path, title="New task")
    interrupted = create_task(tmp_path, title="Halted task")

    new_task.priority = "high"
    interrupted.priority = "low"
    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    save_task(tmp_path, new_task)
    save_task(tmp_path, interrupted)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == interrupted.id


def test_resolve_next_task_priority_first_prefers_high_priority_queue_head(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="priority_first"))
    new_task = create_task(tmp_path, title="New task")
    interrupted = create_task(tmp_path, title="Halted task")

    new_task.priority = "high"
    interrupted.priority = "low"
    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    save_task(tmp_path, new_task)
    save_task(tmp_path, interrupted)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == new_task.id


def test_resolve_next_task_dependency_aware_respects_queue_order_before_interrupted_work(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="dependency_aware"))
    queued = create_task(tmp_path, title="Next head task")
    interrupted = create_task(tmp_path, title="Halted task")

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    save_task(tmp_path, interrupted)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == queued.id


def test_resolve_next_task_dependency_aware_respects_queue_head_before_downstream_count(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_selection_policy="dependency_aware"))
    first = create_task(tmp_path, title="Unrelated ready task")
    root = create_task(tmp_path, title="Dependency root")
    mid = create_task(tmp_path, title="Mid dependency")
    leaf = create_task(tmp_path, title="Leaf dependency")

    mid.depends_on = [root.id]
    leaf.depends_on = [mid.id]
    save_task(tmp_path, mid)
    save_task(tmp_path, leaf)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == first.id


def test_peek_next_task_selection_reports_blocked_dependencies(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked dependent")
    prerequisite = create_task(tmp_path, title="Prerequisite")

    blocked.depends_on = [prerequisite.id, "T-9999"]
    save_task(tmp_path, blocked)

    selection = peek_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == prerequisite.id
    assert [entry.task_id for entry in selection.blocked] == [blocked.id]
    assert selection.blocked[0].blocked_by == [
        f"{prerequisite.id} (queued/backlog)",
        "T-9999 (missing)",
    ]


def test_drain_task_pool_skips_stale_queue_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Real task", auto_commit=False)
    state = load_state(tmp_path)
    state.queue = ["T-9999", task.id]
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [task.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_drain_task_pool_skips_ineligible_active_and_queue_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Flagged active task", auto_commit=False)
    queued = create_task(tmp_path, title="Real task", auto_commit=False)
    completed = create_task(tmp_path, title="Completed prior task", auto_commit=False)

    active.status = "flagged"
    save_task(tmp_path, active)
    completed.status = "done"
    completed.pipeline_status = "done"
    save_task(tmp_path, completed)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [completed.id, queued.id]
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [active.id, queued.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_drain_task_pool_reports_blocked_tasks_remaining(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked task", auto_commit=False)
    missing = "T-9999"

    blocked.depends_on = [missing]
    save_task(tmp_path, blocked)

    summary = drain_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [blocked.id]
    assert summary.blocked[0].blocked_by == [f"{missing} (missing)"]
    assert load_state(tmp_path).queue == [blocked.id]


def test_drain_task_pool_reports_and_requeues_blocked_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked active task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    state = load_state(tmp_path)
    state.active_task_id = blocked.id
    state.queue = []
    save_state(tmp_path, state)

    summary = drain_task_pool(tmp_path)

    assert summary.executions == []
    assert summary.stop_reason == "blocked_tasks_remaining"
    assert [entry.task_id for entry in summary.blocked] == [blocked.id]
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [blocked.id]


def test_drain_task_pool_stops_after_requeueing_interrupted_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Halted task", auto_commit=False)

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "testing":
            raise KeyboardInterrupt()
        return _completed_subagent_result(tmp_path, current_task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)
    summary = drain_task_pool(tmp_path)

    assert summary.executions
    assert summary.executions[0].result is not None
    assert summary.executions[0].result.final_status == "interrupted"
    assert summary.stop_reason == "task_interrupted"
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert load_state(tmp_path).queue == []


def test_runner_requeues_commit_stage_after_keyboard_interrupt(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Halted commit", auto_commit=False)
    task.pipeline_status = "commit_to_git"
    task.status = "in_progress"
    save_task(tmp_path, task)

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "commit_to_git":
            task.status = "done"
            task.pipeline_status = "done"
            raise KeyboardInterrupt()
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok")

    runner = TaskExecutionRunner(tmp_path, executor)
    result = runner.run(task)

    assert result.final_status == "interrupted"
    finish_task_run_transition(tmp_path, task, result.final_status)
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).queue == [task.id]


def test_drain_task_pool_drains_active_task_without_queued_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resumed task", auto_commit=False)
    state = load_state(tmp_path)
    state.active_task_id = task.id
    state.queue = []
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, current_task, role, engine_name, prompt, model=None, max_turns=None: (
            _completed_subagent_result(tmp_path, current_task.pipeline_status)
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [task.id]
    assert summary.stop_reason == "queue_exhausted"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == []


def test_drain_task_pool_stops_after_requeueing_review_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    active = create_task(tmp_path, title="Active review loop", auto_commit=False)
    queued = create_task(tmp_path, title="Waiting behind active", auto_commit=False)
    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.retry_policy.max_retries = 2  # allow 1 testing fail + 1 accepting reject
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        transcript = "\n".join(
            [
                "VERDICT: PASS",
                f"SUMMARY: {task.pipeline_status} passed",
                "FILES_CHANGED:",
                "TESTS_ADDED: 0",
                "TESTS_PASSING: 0",
                "WARNINGS:",
            ]
        )
        if task.id == active.id and task.pipeline_status == "testing":
            transcript = "\n".join(
                [
                    "VERDICT: FAIL",
                    "SUMMARY: qa wants another implementation pass",
                    "FILES_CHANGED:",
                    "TESTS_ADDED: 0",
                    "TESTS_PASSING: 0",
                    "WARNINGS:",
                ]
            )
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.id}-{task.pipeline_status}-codex",
                role=role,
                engine=engine_name,
                status="completed",
                path=f"subagents/{task.id}-{task.pipeline_status}-codex",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=0,
                stdout=transcript,
                stderr="",
            ),
            transcript=transcript,
            exit_code=0,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [active.id]
    assert summary.stop_reason == "task_requeued"
    refreshed_active = get_task(tmp_path, active.id)
    assert refreshed_active is not None
    assert refreshed_active.status == "queued"
    assert refreshed_active.pipeline_status == "implementing"
    assert refreshed_active.runtime.retry_count == 1
    assert refreshed_active.runtime.retry_limit == 2
    assert refreshed_active.runtime.retry_source == "task"
    refreshed_queued = get_task(tmp_path, queued.id)
    assert refreshed_queued is not None
    assert refreshed_queued.status == "queued"
    assert refreshed_queued.pipeline_status == "backlog"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [active.id, queued.id]


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
    assert "Process profile: Rust" in context
    assert "## Init scaffold" in context
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
                "task_engine_routing": {"research": ["opencode", "codex"]},
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
                "task_engine_routing": {"review": ["codex", "copilot"]},
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
    assert config.task_engine_routing["research"] == ["opencode", "codex"]
    assert config.task_engine_routing["review"] == ["codex", "copilot"]
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


def test_resolve_model_prefers_run_override_then_task_then_workspace_default(tmp_path: Path) -> None:
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


def test_litehive_config_merges_partial_task_engine_routing_override() -> None:
    config = LitehiveConfig(task_engine_routing={"research": ["opencode", "gemini", "codex"]})

    assert config.task_engine_routing["research"] == ["opencode", "gemini", "codex"]
    assert config.task_engine_routing["review"] == ["copilot", "codex", "opencode", "gemini"]


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
    assert resolve_execution_retry_policy(
        config,
        engine_name="opencode",
        model_name="zai-coding-plan/glm-5.1",
    ).selector == "opencode"
    assert resolve_execution_retry_policy(
        config,
        engine_name="gemini",
        model_name="gemini-2.5-pro",
    ).selector == "gemini"


def test_resolve_execution_retry_policy_prefers_claude_selector_before_model_family_and_external_cli() -> None:
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


def test_resolve_engine_name_uses_task_routing_rule_before_workspace_default(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "gemini"
    assert resolve_engine_plan(task, config)[:3] == ["gemini", "codex", "opencode"]


def test_resolve_engine_name_prefers_explicit_task_type_over_keyword_inference(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior", task_type="review")

    assert resolve_engine_name(task, config) == "copilot"
    assert resolve_engine_plan(task, config)[:3] == ["copilot", "codex", "opencode"]


def test_resolve_engine_name_uses_configured_task_routing_override(
    tmp_path: Path,
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            task_engine_routing={"research": ["opencode", "gemini", "codex"]},
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine quota behavior")

    assert resolve_engine_name(task, config) == "opencode"
    assert resolve_engine_plan(task, config) == ["opencode", "gemini", "codex"]


def test_resolve_engine_name_skips_claude_in_routing_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_engine="codex",
            task_engine_routing={"research": ["claude", "gemini", "codex"]},
        ),
    )
    config = load_config(tmp_path)
    task = create_task(tmp_path, title="Research engine selection behavior")

    assert resolve_engine_name(task, config) == "claude"
    assert resolve_engine_plan(task, config) == ["claude", "gemini", "codex"]


def test_configure_persists_task_engine_routing_overrides(tmp_path: Path) -> None:
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
        task_engine_route=[
            "research=gemini,claude,codex",
            "bugfix=codex,opencode,copilot",
        ],
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
    assert config.task_engine_routing["research"] == ["gemini", "claude", "codex"]
    assert config.task_engine_routing["bugfix"] == ["codex", "opencode", "copilot"]
    assert config.task_engine_routing["review"] == ["copilot", "codex", "opencode", "gemini"]


def test_configure_persists_pre_acceptance_command(tmp_path: Path) -> None:
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
        pre_acceptance_command="uv run ruff check litehive tests",
        hook=None,
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)

    assert config.pre_acceptance_command == "uv run ruff check litehive tests"
    assert config.runner_hooks["before_pm_acceptance"][0].command == "uv run ruff check litehive tests"
    assert config.runner_hooks["before_pm_acceptance"][0].blocking is True


def test_pre_acceptance_command_forces_matching_runner_hook_to_blocking() -> None:
    config = LitehiveConfig(
        pre_acceptance_command="uv run ruff check litehive tests",
        runner_hooks={
            "before_pm_acceptance": [
                {"command": "uv run ruff check litehive tests", "blocking": False}
            ]
        },
    )

    before_acceptance_hooks = config.runner_hooks["before_pm_acceptance"]
    assert len(before_acceptance_hooks) == 1
    assert before_acceptance_hooks[0].command == "uv run ruff check litehive tests"
    assert before_acceptance_hooks[0].blocking is True


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
        ],
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)

    assert config.runner_hooks["before_swe_implementation"][0].blocking is False
    assert config.runner_hooks["after_swe_implementation"][0].command == "echo post"
    assert config.runner_hooks["before_pm_acceptance"][0].command == "echo review"
    assert config.runner_hooks["after_pm_acceptance"][0].blocking is False


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


def test_configure_rejects_invalid_task_engine_route(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
        task_engine_route=["research=gemini,unknown"],
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
    )

    assert _cmd_configure(parser) == 1
    assert "--task-engine-route engine must be one of:" in capsys.readouterr().out


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
    run_args = parser.parse_args(
        ["run", "--workspace", str(tmp_path), "--model", "gpt-5"]
    )
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


def test_cmd_run_dry_run_shows_planned_tasks_and_stop_conditions_without_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", engine="opencode")

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_single_task should not be called for dry-run")

    def fail_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run_single_task", fail_run_single)
    monkeypatch.setattr("litehive.cli.drain_task_pool", fail_drain)

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

    monkeypatch.setattr("litehive.cli.run_single_task", fail_run_single)
    monkeypatch.setattr("litehive.cli.drain_task_pool", fail_drain)

    exit_code = _cmd_run(
        argparse.Namespace(workspace=tmp_path, dry_run=True, engine="gemini", drain=False))
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
    create_task(tmp_path, title="Pending task", engine="opencode", model="task-model", auto_commit=False)

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


def test_cmd_run_dry_run_plans_dependency_aware_pool_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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

    monkeypatch.setattr("litehive.cli.drain_task_pool", fail_drain_task_pool)

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
    assert "engine_attempts=gemini, codex, opencode, copilot" in output
    assert "predicted_stop_reason: single_task_complete" in output


def test_drain_task_pool_uses_run_engine_override_for_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", engine="codex", auto_commit=False)
    seen_engines: list[str] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        seen_engines.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

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

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        seen_engines.append(engine_name)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

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
    create_task(tmp_path, title="Pending task", engine="opencode", model="task-model", auto_commit=False)
    seen_models: list[str | None] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        seen_models.append(model)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    run_single_task(tmp_path, model_override="run-model")
    assert seen_models == ["run-model", "run-model", "run-model", "run-model"]

    seen_models.clear()
    create_task(tmp_path, title="Pending task 2", engine="opencode", model="task-model-2", auto_commit=False)
    run_single_task(tmp_path)
    assert seen_models == ["task-model-2", "task-model-2", "task-model-2", "task-model-2"]

    seen_models.clear()
    create_task(tmp_path, title="Pending task 3", engine="opencode", auto_commit=False)
    run_single_task(tmp_path)
    assert seen_models == ["workspace-model", "workspace-model", "workspace-model", "workspace-model"]


def test_run_single_task_does_not_pass_model_override_to_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", engine="codex", model="task-model", auto_commit=False)
    seen_models: list[str | None] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        seen_models.append(model)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

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
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert summary.stop_reason == "queue_exhausted"
    assert [execution.task.id for execution in summary.executions if execution.task is not None] == [
        "T-0001",
        "T-0002",
    ]


def test_run_task_rejects_starting_a_second_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    pending = create_task(tmp_path, title="Pending task", auto_commit=False)

    active.runtime.execution_status = "running"
    save_task_runtime(tmp_path, active)
    state = load_state(tmp_path)
    state.active_task_id = active.id
    save_state(tmp_path, state)

    with pytest.raises(
        WorkspaceConflictError,
        match=f"task {pending.id} cannot start because task {active.id} is already active",
    ):
        run_task(tmp_path, pending)


def test_dequeue_next_task_selection_rejects_multiple_active_tasks(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First active task", auto_commit=False)
    second = create_task(tmp_path, title="Second active task", auto_commit=False)

    first.runtime.execution_status = "running"
    second.runtime.execution_status = "running"
    save_task_runtime(tmp_path, first)
    save_task_runtime(tmp_path, second)

    with pytest.raises(WorkspaceConflictError, match="workspace has multiple active tasks"):
        dequeue_next_task_selection(tmp_path)


def test_set_active_task_rejects_starting_a_second_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task", auto_commit=False)
    pending = create_task(tmp_path, title="Pending task", auto_commit=False)

    active.runtime.execution_status = "running"
    save_task_runtime(tmp_path, active)

    with pytest.raises(
        WorkspaceConflictError,
        match="workspace has multiple active tasks",
    ):
        set_active_task(tmp_path, pending.id)


def test_cmd_run_default_executes_single_task_and_reports_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 First task" in output
    assert "task: T-0002 Second task" not in output
    assert (
        "stage_outcomes: grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert "completed_tasks: 1" in output
    assert (
        "completed: T-0001 First task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert "remaining_tasks: 1" in output
    assert "tasks_run: 1" in output
    assert "stop_reason: single_task_complete" in output
    summary_report = (tmp_path / ".litehive" / "pool-summary.txt").read_text(encoding="utf-8")
    assert "completed_tasks: 1" in summary_report
    assert (
        "completed: T-0001 First task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in summary_report
    )
    assert "stop_reason: single_task_complete" in summary_report
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["stop_reason"] == "single_task_complete"
    assert durable_report["stop_condition"] == "single task complete"
    assert durable_report["tasks_run"] == 1
    assert durable_report["completed_count"] == 1
    assert durable_report["flagged_count"] == 0
    assert durable_report["skipped_count"] == 1
    assert durable_report["remaining_count"] == 1
    assert durable_report["completed"] == [
        {
            "task_id": "T-0001",
            "title": "First task",
            "final_task_status": "done",
            "pipeline_status": "done",
            "stage_outcomes": [
                "grooming=pass",
                "implementing=pass",
                "testing=pass",
                "accepting=pass",
                "commit_to_git=pass",
            ],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        }
    ]
    assert durable_report["skipped"] == [
        {
            "task_id": "T-0002",
            "title": "Second task",
            "final_task_status": "queued",
            "pipeline_status": "backlog",
            "stage_outcomes": [],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        }
    ]
    assert load_state(tmp_path).queue == ["T-0002"]


def test_cmd_run_drains_task_pool_and_reports_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 First task" in output
    assert "task: T-0002 Second task" in output
    assert "completed_tasks: 2" in output
    assert "tasks_run: 2" in output
    assert "stop_reason: queue_exhausted" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["stop_reason"] == "queue_exhausted"
    assert durable_report["stop_condition"] == "queue exhausted"
    assert durable_report["tasks_run"] == 2
    assert durable_report["completed_count"] == 2
    assert durable_report["flagged_count"] == 0
    assert durable_report["skipped_count"] == 0
    assert durable_report["remaining_count"] == 0
    assert durable_report["completed"] == [
        {
            "task_id": "T-0001",
            "title": "First task",
            "final_task_status": "done",
            "pipeline_status": "done",
            "stage_outcomes": [
                "grooming=pass",
                "implementing=pass",
                "testing=pass",
                "accepting=pass",
                "commit_to_git=pass",
            ],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        },
        {
            "task_id": "T-0002",
            "title": "Second task",
            "final_task_status": "done",
            "pipeline_status": "done",
            "stage_outcomes": [
                "grooming=pass",
                "implementing=pass",
                "testing=pass",
                "accepting=pass",
                "commit_to_git=pass",
            ],
            "reason_code": None,
            "reason": None,
            "follow_up_task_id": None,
        },
    ]
    assert load_state(tmp_path).queue == []


def test_cmd_run_reports_runner_conflict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)

    from litehive import tasks as tasks_module

    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.tasks.fcntl.flock", fake_flock)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=False,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "run failed: workspace is already being mutated by another runner" in output


def test_save_task_rejects_runner_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Pending task", auto_commit=False)

    from litehive import tasks as tasks_module

    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.tasks.fcntl.flock", fake_flock)

    task.title = "Updated title"
    with pytest.raises(
        WorkspaceConflictError,
        match="workspace is already being mutated by another runner",
    ):
        save_task(tmp_path, task)


def test_save_state_rejects_runner_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    state = load_state(tmp_path)

    from litehive import tasks as tasks_module

    real_flock = tasks_module.fcntl.flock

    def fake_flock(fd, flags):  # type: ignore[no-untyped-def]
        if flags & tasks_module.fcntl.LOCK_NB:
            raise BlockingIOError("runner is busy")
        return real_flock(fd, flags)

    monkeypatch.setattr("litehive.tasks.fcntl.flock", fake_flock)

    with pytest.raises(
        WorkspaceConflictError,
        match="workspace is already being mutated by another runner",
    ):
        save_state(tmp_path, state)


def test_cmd_run_reports_blocked_tasks_when_no_runnable_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    blocked = create_task(tmp_path, title="Blocked task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No runnable task." in output
    assert f"blocked: {blocked.id} Blocked task blocked_by=T-9999 (missing)" in output
    assert "completed_tasks: 0" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert "remaining_tasks: 1" in output
    assert (
        f"remaining: {blocked.id} Blocked task status=queued pipeline_status=backlog" in output
    )
    assert "tasks_run: 0" in output
    assert "progress_status: no_useful_progress" in output
    assert (
        "summary: Pool stopped with no useful progress because no runnable task remained."
        in output
    )
    assert "stop_reason: blocked_tasks_remaining" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["progress_status"] == "no_useful_progress"
    assert (
        durable_report["summary"]
        == "Pool stopped with no useful progress because no runnable task remained."
    )


def test_cmd_run_reports_pre_execution_stop_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            stop_on_dirty_git=True,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No task executed." in output
    assert "completed_tasks: 0" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert "remaining_tasks: 1" in output
    assert "remaining: T-0001 Pending task status=queued pipeline_status=backlog" in output
    assert "tasks_run: 0" in output
    assert "stop_condition: dirty git state" in output
    assert "stop_reason: dirty_git_state" in output


def test_cmd_run_reports_resumable_interrupted_tasks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Halted task", auto_commit=False)
    create_task(tmp_path, title="Pending follow-up", auto_commit=False)

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "testing":
            raise KeyboardInterrupt()
        return _completed_subagent_result(tmp_path, current_task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: interrupted" in output
    assert "resumable_tasks: 1" in output
    assert "resumable: T-0001 Halted task status=interrupted pipeline_status=testing" in output
    assert "reason_code=execution_interrupted" in output
    assert "remaining_tasks: 1" in output
    assert "remaining: T-0002 Pending follow-up status=queued pipeline_status=backlog" in output
    assert "progress_status: no_useful_progress" in output
    assert (
        "summary: Pool stopped with no useful progress because the active task was interrupted and must be resumed."
        in output
    )
    assert "stop_condition: task interrupted and awaiting resume" in output
    assert "stop_reason: task_interrupted" in output


def test_run_next_task_marks_subagent_termination_as_interrupted(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Halted subagent task", auto_commit=False)

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "testing":
            return _interrupted_subagent_result(tmp_path, current_task.pipeline_status, engine_name=engine_name)
        return _completed_subagent_result(tmp_path, current_task.pipeline_status, engine_name=engine_name)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)
    try:
        summary = run_next_task(tmp_path)
    finally:
        monkeypatch.undo()

    assert summary.result is not None
    assert summary.result.final_status == "interrupted"
    refreshed = require_task(tmp_path, task.id)
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.runtime.last_outcome.reason_code == "execution_interrupted"
    assert refreshed.runtime.current_stage.step == "testing"
    assert refreshed.runtime.current_stage.status == "interrupted"


def test_classify_execution_interruption_matches_signal_exit_codes_and_text() -> None:
    assert classify_execution_interruption("", exit_code=130) == "execution interrupted"
    assert classify_execution_interruption("Received SIGINT from controlling terminal") == "execution interrupted"
    assert classify_execution_interruption("ordinary failure", exit_code=1) is None


def test_cmd_run_reports_remaining_tasks_when_pool_stops_early(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=1,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=False,
            drain=True,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "completed_tasks: 1" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 1" in output
    assert (
        "completed: T-0001 First task status=done pipeline_status=done "
        "stage_outcomes=grooming=pass, implementing=pass, testing=pass, accepting=pass, commit_to_git=pass"
        in output
    )
    assert (
        "skipped: T-0002 Second task status=queued pipeline_status=backlog stage_outcomes=-"
        in output
    )
    assert "remaining_tasks: 1" in output
    assert (
        "remaining: T-0002 Second task status=queued pipeline_status=backlog stage_outcomes=-"
        in output
    )
    assert "tasks_run: 1" in output
    assert "stop_condition: max tasks reached" in output
    assert "stop_reason: max_tasks_reached" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["stop_reason"] == "max_tasks_reached"
    assert durable_report["stop_condition"] == "max tasks reached"
    assert durable_report["tasks_run"] == 1
    assert durable_report["completed_count"] == 1
    assert durable_report["flagged_count"] == 0
    assert durable_report["skipped_count"] == 1
    assert durable_report["remaining_count"] == 1


def test_cmd_run_drain_reports_no_useful_progress_after_requeue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    active = create_task(tmp_path, title="Active review loop", auto_commit=False)
    queued = create_task(tmp_path, title="Waiting behind active", auto_commit=False)
    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.retry_policy.max_retries = 2
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        transcript = "\n".join(
            [
                "VERDICT: PASS",
                f"SUMMARY: {task.pipeline_status} passed",
                "FILES_CHANGED:",
                "TESTS_ADDED: 0",
                "TESTS_PASSING: 0",
                "WARNINGS:",
            ]
        )
        if task.id == active.id and task.pipeline_status == "testing":
            transcript = "\n".join(
                [
                    "VERDICT: FAIL",
                    "SUMMARY: qa wants another implementation pass",
                    "FILES_CHANGED:",
                    "TESTS_ADDED: 0",
                    "TESTS_PASSING: 0",
                    "WARNINGS:",
                ]
            )
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.id}-{task.pipeline_status}-codex",
                role=role,
                engine=engine_name,
                status="completed",
                path=f"subagents/{task.id}-{task.pipeline_status}-codex",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=0,
                stdout=transcript,
                stderr="",
            ),
            transcript=transcript,
            exit_code=0,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "progress_status: no_useful_progress" in output
    assert (
        "summary: Pool stopped with no useful progress because the active task was requeued for another pass."
        in output
    )
    assert "stop_reason: task_requeued" in output
    durable_report = _latest_pool_run_report(tmp_path)
    assert durable_report["progress_status"] == "no_useful_progress"
    assert (
        durable_report["summary"]
        == "Pool stopped with no useful progress because the active task was requeued for another pass."
    )


def test_cmd_run_reports_human_checkpoint_stop_without_marking_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(
        tmp_path,
        title="Review before acceptance",
        human_checkpoints=["before_acceptance"],
        auto_commit=False,
    )
    create_task(tmp_path, title="Second task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: paused" in output
    assert "completed_tasks: 0" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 2" in output
    assert "tasks_run: 1" in output
    assert "stop_condition: human checkpoint before acceptance" in output
    assert "stop_reason: human_checkpoint_before_acceptance" in output


def test_cmd_run_reports_requeued_task_even_when_other_tasks_are_blocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_retry_limit=0))
    active = create_task(tmp_path, title="Active review loop", auto_commit=False)
    blocked = create_task(tmp_path, title="Blocked later task", auto_commit=False)
    blocked.depends_on = ["T-9999"]
    save_task(tmp_path, blocked)

    active.status = "in_progress"
    active.pipeline_status = "testing"
    active.retry_policy.max_retries = 2
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [blocked.id]
    save_state(tmp_path, state)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        transcript = "\n".join(
            [
                "VERDICT: PASS",
                f"SUMMARY: {task.pipeline_status} passed",
                "FILES_CHANGED:",
                "TESTS_ADDED: 0",
                "TESTS_PASSING: 0",
                "WARNINGS:",
            ]
        )
        if task.id == active.id and task.pipeline_status == "testing":
            transcript = "\n".join(
                [
                    "VERDICT: FAIL",
                    "SUMMARY: qa wants another implementation pass",
                    "FILES_CHANGED:",
                    "TESTS_ADDED: 0",
                    "TESTS_PASSING: 0",
                    "WARNINGS:",
                ]
            )
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{task.id}-{task.pipeline_status}-codex",
                role=role,
                engine=engine_name,
                status="completed",
                path=f"subagents/{task.id}-{task.pipeline_status}-codex",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=0,
                stdout=transcript,
                stderr="",
            ),
            transcript=transcript,
            exit_code=0,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Active review loop" in output
    assert "status: queued" in output
    assert "No runnable task." not in output
    assert "tasks_run: 1" in output
    assert "stop_reason: task_requeued" in output
    assert (
        f"remaining: {blocked.id} Blocked later task status=queued pipeline_status=backlog"
        in output
    )


def test_dequeue_next_task_selection_restores_missing_queued_tasks_to_state_queue(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First queued task")
    second = create_task(tmp_path, title="Second queued task")

    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    assert selection.task.id == first.id
    repaired_state = load_state(tmp_path)
    assert repaired_state.active_task_id == first.id
    assert second.id in repaired_state.queue


def test_cmd_run_reports_stage_outcomes_for_remaining_task_with_prior_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="First task", auto_commit=False)
    second = create_task(tmp_path, title="Second task", auto_commit=False)
    reports_dir = task_dir(tmp_path, second) / "reports"
    reports_dir.mkdir(exist_ok=True)
    (reports_dir / "grooming-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": second.id,
                "step": "grooming",
                "verdict": "pass",
                "summary": "groomed",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=1,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=False,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "skipped: T-0002 Second task status=queued pipeline_status=backlog "
        "stage_outcomes=grooming=pass"
        in output
    )
    assert (
        "remaining: T-0002 Second task status=queued pipeline_status=backlog "
        "stage_outcomes=grooming=pass"
        in output
    )


def test_cmd_run_reports_failed_task_summary_with_stage_outcomes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Needs acceptance criteria", auto_commit=False)
    task.pipeline_status = "implementing"
    task.priority = "high"
    save_task(tmp_path, task)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "flagged_tasks: 1" in output
    assert (
        "flagged: T-0001 Needs acceptance criteria status=flagged pipeline_status=implementing "
        "stage_outcomes=implementing=blocked"
        in output
    )
    assert "skipped_tasks: 0" in output
    assert "remaining_tasks: 0" in output
    assert "tasks_run: 1" in output
    assert "stop_reason: single_task_complete" in output


def test_cmd_run_uses_configured_pool_stop_defaults(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(pool_stop_on_dirty_git=True))
    _init_git_repo(tmp_path)
    create_task(tmp_path, title="Pending task", auto_commit=False)
    (tmp_path / "app.txt").write_text("changed\n", encoding="utf-8")

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            stop_on_dirty_git=False,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No task executed." in output
    assert "stop_reason: dirty_git_state" in output


def test_cmd_run_reports_summary_when_queue_is_empty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            dry_run=False,
            engine=None,
            stop_on_failure=False,
            max_tasks=None,
            stop_on_limit=False,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=False,
            drain=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No queued task." in output
    assert "completed_tasks: 0" in output
    assert "flagged_tasks: 0" in output
    assert "skipped_tasks: 0" in output
    assert "remaining_tasks: 0" in output
    assert "tasks_run: 0" in output
    assert "stop_condition: queue exhausted" in output
    assert "stop_reason: queue_exhausted" in output
    summary_report = (tmp_path / ".litehive" / "pool-summary.txt").read_text(encoding="utf-8")
    assert "completed_tasks: 0" in summary_report
    assert "flagged_tasks: 0" in summary_report
    assert "stop_condition: queue exhausted" in summary_report


def test_status_output_includes_runtime_observability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            default_retry_limit=2,
            execution_retry_policies={
                "external_cli": {
                    "max_retries": 2,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout", "network"],
                }
            },
            pool_stop_on_failure=True,
            pool_max_tasks=4,
            pool_stop_on_execution_limit=True,
            pool_quota_threshold=2,
            pool_budget_threshold=1,
            pool_stop_on_dirty_git=True,
            pool_selection_policy="priority_first",
        ),
    )
    task = create_task(tmp_path, title="Observe long run")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.retry_policy.max_retries = 1
    task.runtime.execution_status = "running"
    task.runtime.retry_count = 1
    task.runtime.retry_limit = 1
    task.runtime.retry_source = "task"
    task.runtime.run_started_at = "2026-03-31T10:00:00+00:00"
    task.runtime.current_stage = RuntimeStageState(
        step="implementing",
        status="running",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:01+00:00",
        duration_seconds=0,
        summary="",
    )
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="completed",
        path="subagents/SA-0001-swe",
        pid=4242,
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:02:00+00:00",
        completed_at="2026-03-31T10:02:00+00:00",
        exit_code=0,
        transcript_snippet="implemented live observability",
    )
    task.runtime.last_stage = RuntimeStageState(
        step="grooming",
        status="completed",
        started_at="2026-03-31T09:59:00+00:00",
        completed_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
        duration_seconds=60,
        verdict="pass",
        summary="plan confirmed",
    )
    task.runtime.last_outcome.kind = "blocked"
    task.runtime.last_outcome.stage = "testing"
    task.runtime.last_outcome.reason_code = "verdict_blocked"
    task.runtime.last_outcome.reason = "waiting on fixture update"
    task.runtime.last_outcome.retry_count = 1
    task.runtime.last_outcome.retry_limit = 1
    task.runtime.last_outcome.retry_source = "task"
    task.runtime.last_outcome.recorded_at = "2026-03-31T10:02:30+00:00"

    save_task(tmp_path, task)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": os.getpid()}, sort_keys=False),
        encoding="utf-8",
    )
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "default_retry_limit: 2" in output
    assert (
        "external_cli=retries:2 backoff:0.25s "
        "multiplier:2.00 retry_on:timeout,network" in output
    )
    assert "pool_stop_on_failure: True" in output
    assert "pool_max_tasks: 4" in output
    assert "pool_stop_on_execution_limit: True" in output
    assert "pool_quota_threshold: 2" in output
    assert "pool_budget_threshold: 1" in output
    assert "pool_stop_on_dirty_git: True" in output
    assert "pool_selection_policy: priority_first" in output
    assert "pool_stop_reason: None" in output
    assert "process_profile: generic" in output
    assert "retry_limit=1" in output
    assert "auto_commit=True" in output
    assert "commit_message=litehive: complete T-0001 observe-long-run" in output
    assert "retry_policy=configured:1 effective:1 source=task" in output
    assert "run=running" in output
    assert "retries=1/1" in output
    assert "retry_source=task" in output
    assert "stage=implementing" in output
    assert (
        "last_subagent=SA-0001 swe/codex completed pid=4242 sandbox=host snippet=implemented live observability"
        in output
    )
    assert "last_report=grooming/pass duration=1m00s summary=plan confirmed" in output
    assert (
        "outcome=blocked stage=testing reason_code=verdict_blocked recorded_at=2026-03-31T10:02:30+00:00 follow_up_task=- retry_state=1/1 retry_source=task reason=waiting on fixture update"
        in output
    )


def test_render_task_summary_includes_active_subagent_pid() -> None:
    task = TaskRecord(id="T-0001", slug="observe-pid", title="Observe PID")
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
        pid=31337,
        sandboxed=True,
        sandbox_summary="sandbox[docker:test net=none workspace=rw]",
        started_at="2026-03-31T10:00:00+00:00",
        updated_at="2026-03-31T10:00:00+00:00",
    )

    lines = render_task_summary(task, active=False)

    assert any("subagent=SA-0001 swe/codex running pid=31337" in line for line in lines)
    assert any("sandbox=sandbox[docker:test net=none workspace=rw]" in line for line in lines)


def test_cmd_status_includes_engine_monitoring_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_monitoring: codex source=local invocations=1 success=0 failure=1 limits=1" in output
    assert "last_limit_reason=usage limit reached" in output
    assert "usage=used=1,unit=requests" in output


def test_cmd_status_includes_codex_provider_limit_monitoring(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
            stdout='{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"type\\":\\"rate_limit_error\\",\\"message\\":\\"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits.\\"}}"}',
            stderr="",
        ),
        failure_kind="execution_limit",
        failure_reason="usage limit reached",
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_monitoring: codex source=provider invocations=1 success=0 failure=1 limits=1" in output
    assert "provider=openai" in output
    assert "last_limit_reason=usage limit reached" in output


def test_cmd_status_includes_claude_provider_limit_monitoring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_monitoring: claude source=provider invocations=1 success=0 failure=1 limits=1" in output
    assert "provider=anthropic" in output
    assert "last_limit_reason=rate limit reached" in output


def test_cmd_status_includes_gemini_provider_limit_monitoring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="gemini",
        adapter=get_engine("gemini"),
        execution=CLIExecutionResult(
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
        ),
        failure_kind="execution_limit",
        failure_reason="quota limit reached",
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_monitoring: gemini source=provider invocations=1 success=0 failure=1 limits=1" in output
    assert "provider=google" in output
    assert "last_limit_reason=quota limit reached" in output
    assert "usage=limit=2,unit=requests" in output


def test_cmd_status_includes_engine_usage_window(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)

    class ProviderAdapter(ExternalCLIAdapter):
        def build_command(self, prompt: str, cwd: Path, model: str | None = None, *, max_turns: int | None = None) -> list[str]:  # type: ignore[override]
            return ["provider-cli", prompt]

        def extract_usage_observation(self, execution: CLIExecutionResult) -> EngineUsageObservation | None:
            return EngineUsageObservation(
                source="provider",
                provider="github",
                success=True,
                usage=EngineUsageWindow(
                    used=60,
                    limit=100,
                    remaining=40,
                    unit="requests",
                    reset_at="2026-04-30T00:00:00Z",
                ),
            )

    record_engine_execution(
        tmp_path,
        task_id="T-0001",
        engine_name="copilot",
        adapter=ProviderAdapter(
            name="copilot",
            binary="provider-cli",
            capabilities=AdapterCapabilities(supports_model_override=True, transcript_format="jsonl"),
        ),
        execution=CLIExecutionResult(
            adapter="copilot",
            argv=("provider-cli", "run"),
            cwd=tmp_path,
            exit_code=0,
            stdout="{}",
            stderr="",
        ),
        failure_kind=None,
        failure_reason=None,
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "engine_monitoring: copilot source=provider invocations=1 success=1 failure=0 limits=0" in output
    assert "usage=used=60,limit=100,remaining=40,unit=requests,reset_at=2026-04-30T00:00:00Z" in output


def test_render_task_summary_includes_interruption_context() -> None:
    task = TaskRecord(id="T-0001", slug="resume-task", title="Resume task")
    task.status = "interrupted"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "interrupted"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T10:00:00+00:00",
        completed_at="2026-04-01T10:02:00+00:00",
        updated_at="2026-04-01T10:02:00+00:00",
    )
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="interrupted",
        path="subagents/SA-0001-qa",
        pid=5151,
        started_at="2026-04-01T10:00:10+00:00",
        updated_at="2026-04-01T10:02:00+00:00",
        completed_at="2026-04-01T10:02:00+00:00",
        transcript_snippet="halfway through targeted testing",
        interruption_reason="execution interrupted",
    )
    task.runtime.interruption = RuntimeInterruptionState(
        source="subagent",
        stage="testing",
        pipeline_status="testing",
        resume_stage="testing",
        reason="execution interrupted",
        summary="Execution interrupted during testing",
        interrupted_at="2026-04-01T10:02:00+00:00",
        detected_at="2026-04-01T10:02:00+00:00",
        subagent=task.runtime.last_subagent,
    )

    lines = render_task_summary(task, active=False)

    assert any("last_subagent_interruption_reason=execution interrupted" in line for line in lines)
    assert any(
        "interruption=subagent stage=testing resume_from=testing interrupted_at=2026-04-01T10:02:00+00:00"
        in line
        for line in lines
    )


def test_format_external_engine_sandbox_renders_engine_policies() -> None:
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            image="ghcr.io/example/litehive-sandbox:latest",
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    network_mode="none",
                    workspace_mode="rw",
                    environment=["OPENAI_API_KEY"],
                )
            },
        )
    )

    rendered = format_external_engine_sandbox(config)

    assert "enabled runtime:docker image:ghcr.io/example/litehive-sandbox:latest" in rendered
    assert "codex=enabled:True net:none workspace:rw env:OPENAI_API_KEY creds:-" in rendered


def test_format_subagent_resource_limits_renders_effective_limits() -> None:
    rendered = format_subagent_resource_limits(LitehiveConfig(process_profile="rust"))

    assert rendered == "enabled memory_mb:8192 cpu_count:4 process_limit:512"


def test_status_output_includes_default_execution_retry_policies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert (
        "execution_retry_policies: claude=retries:2 backoff:0.25s multiplier:2.00 "
        "retry_on:timeout,network,service; codex=retries:2 backoff:0.25s multiplier:2.00 "
        "retry_on:timeout,network,service; gemini=retries:2 backoff:0.25s "
        "multiplier:2.00 retry_on:timeout,network,service; opencode=retries:2 "
        "backoff:0.25s multiplier:2.00 retry_on:timeout,network,service"
    ) in output


def test_status_output_includes_external_engine_sandbox_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            external_engine_sandbox=ExternalEngineSandboxConfig(
                enabled=True,
                image="ghcr.io/example/litehive-sandbox:latest",
                engine_policies={
                    "codex": ExternalEngineSandboxPolicy(
                        enabled=True,
                        network_mode="none",
                        workspace_mode="rw",
                        environment=["OPENAI_API_KEY"],
                    )
                },
            )
        ),
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.external_engine_sandbox.enabled is True
    assert config.external_engine_sandbox.image == "ghcr.io/example/litehive-sandbox:latest"
    assert "codex" in config.external_engine_sandbox.engine_policies
    codex_policy = config.external_engine_sandbox.engine_policies["codex"]
    assert codex_policy.enabled is True
    assert codex_policy.network_mode == "none"
    assert codex_policy.workspace_mode == "rw"
    assert "OPENAI_API_KEY" in codex_policy.environment


def test_status_output_includes_subagent_resource_limits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(process_profile="rust"))

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.subagent_resource_limits.enabled is True
    assert config.subagent_resource_limits.memory_mb == 8192
    assert config.subagent_resource_limits.cpu_count == 4
    assert config.subagent_resource_limits.process_limit == 512


def test_status_output_includes_runner_hooks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "before_swe_implementation": [{"command": "echo pre", "blocking": False}],
                "before_pm_acceptance": [{"command": "echo review", "blocking": True}],
            }
        ),
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert "before_pm_acceptance" in config.runner_hooks
    assert config.runner_hooks["before_pm_acceptance"][0].command == "echo review"
    assert config.runner_hooks["before_pm_acceptance"][0].blocking is True
    assert "before_swe_implementation" in config.runner_hooks
    assert config.runner_hooks["before_swe_implementation"][0].command == "echo pre"
    assert config.runner_hooks["before_swe_implementation"][0].blocking is False


def test_status_output_includes_budget_control_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            pool_usage_cap=12,
            pool_cost_cap=30,
            engine_usage_caps={"claude": 2, "codex": 5},
            engine_budget_caps={"claude": 6},
            engine_costs={"claude": 3, "codex": 1},
        ),
    )

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    config = load_config(tmp_path)
    assert config.pool_usage_cap == 12
    assert config.pool_cost_cap == 30
    assert config.engine_usage_caps == {"claude": 2, "codex": 5}
    assert config.engine_budget_caps == {"claude": 6}
    assert config.engine_costs["claude"] == 3
    assert config.engine_costs["codex"] == 1


def test_queue_command_shows_active_and_queued_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task", engine="opencode")
    second.depends_on = [first.id]
    save_task(tmp_path, second)

    set_active_task(tmp_path, first.id)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": os.getpid()}, sort_keys=False),
        encoding="utf-8",
    )
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"active_task_id: {first.id}" in output
    assert (
        f"active: {first.id} [in_progress/backlog] priority=medium engine=codex (default) model=default "
        "title=First task depends_on=-"
    ) in output
    assert (
        f"1. {second.id} [queued/backlog] priority=medium engine=opencode model=default "
        f"title=Second task depends_on={first.id}"
    ) in output


def test_repair_command_repairs_stale_runner_state_and_cleans_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)
    queued = create_task(tmp_path, title="Pending task", auto_commit=False)
    done = create_task(tmp_path, title="Completed task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)

    done.status = "done"
    done.pipeline_status = "done"
    save_task(tmp_path, done)

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id, "T-9999", queued.id, done.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": 999999}, sort_keys=False),
        encoding="utf-8",
    )

    exit_code = _cmd_repair(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "repaired: yes" in output
    assert "stale_runner_recovered: yes" in output
    assert f"cleared_active_task_id: {interrupted.id}" in output
    assert "requeued_tasks: -" in output
    assert f"removed_queue_entries: T-9999 {done.id}" in output
    assert f"deduped_queue_entries: {queued.id}" in output
    assert f"restored_queue_entries: {interrupted.id}" in output
    assert "finalized_commit_tasks: -" in output
    assert "active_task_id: None" in output
    assert "queue_length: 2" in output

    repaired_state = load_state(tmp_path)
    assert repaired_state.active_task_id is None
    assert repaired_state.queue == [queued.id, interrupted.id]
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"


def test_repair_workspace_state_reports_noop_for_consistent_workspace(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Pending task", auto_commit=False)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is False
    assert summary.stale_runner_recovered is False
    assert summary.cleared_active_task_id is None
    assert summary.requeued_task_ids == []
    assert summary.removed_queue_entries == []
    assert summary.deduped_queue_entries == []
    assert summary.restored_queue_entries == []
    assert summary.finalized_commit_task_ids == []
    assert load_state(tmp_path).queue == [task.id]


def test_repair_workspace_state_requeues_untouched_active_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Halted active task", auto_commit=False)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)

    active.status = "in_progress"
    active.pipeline_status = "testing"
    save_task(tmp_path, active)

    state = load_state(tmp_path)
    state.active_task_id = active.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.stale_runner_recovered is False
    assert summary.cleared_active_task_id == active.id
    assert summary.requeued_task_ids == [active.id]
    assert summary.removed_queue_entries == []
    assert summary.deduped_queue_entries == []
    assert summary.restored_queue_entries == []
    assert summary.finalized_commit_task_ids == []

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue == [queued.id, active.id]

    refreshed = get_task(tmp_path, active.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "idle"


def test_repair_workspace_state_requeues_orphaned_commit_stage_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    orphaned = create_task(tmp_path, title="Halted commit task", auto_commit=False)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)

    orphaned.status = "in_progress"
    orphaned.pipeline_status = "commit_to_git"
    orphaned.runtime.execution_status = "running"
    orphaned.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, orphaned)
    save_task_runtime(tmp_path, orphaned)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [queued.id]
    save_state(tmp_path, state)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.stale_runner_recovered is True
    assert summary.cleared_active_task_id is None
    assert summary.requeued_task_ids == [orphaned.id]
    assert summary.removed_queue_entries == []
    assert summary.deduped_queue_entries == []
    assert summary.restored_queue_entries == []
    assert summary.finalized_commit_task_ids == []

    refreshed = get_task(tmp_path, orphaned.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, queued.id]


def test_repair_workspace_state_finalizes_existing_checkpoint_commit(tmp_path: Path) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit task")
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    commit_message = "litehive: complete T-0001 stranded-commit-task"
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_message], tmp_path)
    existing_checkpoint_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)

    stranded.status = "done"
    stranded.pipeline_status = "done"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = initial_sha
    stranded.git.commit_sha = None
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.stale_runner_recovered is True
    assert summary.cleared_active_task_id is None
    assert summary.requeued_task_ids == []
    assert summary.removed_queue_entries == []
    assert summary.deduped_queue_entries == []
    assert summary.restored_queue_entries == []
    assert summary.finalized_commit_task_ids == [stranded.id]

    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_checkpoint_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == [queued.id]


def test_queue_command_marks_recovered_interruption(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": 999999}, sort_keys=False),
        encoding="utf-8",
    )

    exit_code = _cmd_queue(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "active_task_id: None" in output
    assert "queue_length: 1" in output
    assert "resumable_tasks: 1" in output
    assert (
        f"resume 1. {interrupted.id} [interrupted/testing] priority=medium engine=codex (default) model=default "
        "title=Halted testing task depends_on=- resumable_from=testing interruption=runner "
        "reason_code=execution_interrupted reason=Stale runner detected while `testing` was still marked running."
    ) in output
    assert (
        f"1. {queued.id} [queued/backlog] priority=medium engine=codex (default) model=default "
        "title=Pending follow-up depends_on=-"
    ) in output


def test_recover_stale_runner_state_recovers_running_task_without_runner_lock_record(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.run_started_at = "2026-04-01T00:00:00+00:00"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    interrupted.subagents.append(
        SubagentRef(
            id="SA-0001",
            role="qa",
            engine="codex",
            status="running",
            path="subagents/SA-0001-qa",
        )
    )
    interrupted.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
        pid=4242,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:00:10+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)
    subagent_base = task_dir(tmp_path, interrupted) / "subagents" / "SA-0001-qa"
    subagent_base.mkdir(parents=True, exist_ok=False)
    (subagent_base / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "status": "running",
                "summary": "halfway through targeted testing",
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
                "warnings": [],
                "resource_limit_event": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (subagent_base / "transcript.md").write_text(
        "VERDICT: PASS\nSUMMARY: halfway through targeted testing\n",
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is True
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.last_subagent is not None
    assert refreshed.runtime.last_subagent.status == "interrupted"
    assert refreshed.runtime.last_subagent.transcript_snippet == "halfway through targeted testing"
    assert refreshed.runtime.last_subagent.interruption_reason.startswith("Stale runner detected while subagent")
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.source == "subagent"
    assert refreshed.runtime.interruption.resume_stage == "testing"
    assert refreshed.runtime.interruption.subagent is not None
    assert refreshed.runtime.interruption.subagent.id == "SA-0001"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert refreshed.subagents[-1].status == "interrupted"
    session = yaml.safe_load((subagent_base / "session.yaml").read_text(encoding="utf-8"))
    report = yaml.safe_load((subagent_base / "report.yaml").read_text(encoding="utf-8"))
    assert session["status"] == "interrupted"
    assert session["resume_stage"] == "testing"
    assert session["interruption_reason"].startswith("Stale runner detected while subagent")
    assert report["status"] == "interrupted"
    assert report["resume_stage"] == "testing"
    assert report["interruption_reason"].startswith("Stale runner detected while subagent")
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted subagent execution while `testing` was running." in journal
    assert "Subagent `SA-0001` (qa/codex, pid=4242, path `subagents/SA-0001-qa`) stopped" in journal
    assert "Resume from `testing`." in journal


def test_recover_stale_runner_state_recovers_when_lock_is_not_held_even_if_pid_is_alive(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    interrupted = create_task(tmp_path, title="Halted testing task", auto_commit=False)

    interrupted.status = "in_progress"
    interrupted.pipeline_status = "testing"
    interrupted.runtime.execution_status = "running"
    interrupted.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, interrupted)
    save_task_runtime(tmp_path, interrupted)

    state = load_state(tmp_path)
    state.active_task_id = interrupted.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": os.getpid(), "started_at": "2026-04-01T00:00:00+00:00"}, sort_keys=False),
        encoding="utf-8",
    )

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is True
    refreshed = get_task(tmp_path, interrupted.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.source == "runner"
    assert refreshed.runtime.interruption.reason == "Stale runner detected while `testing` was still marked running."
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [queued.id]


def test_recover_stale_runner_state_skips_live_runner_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    running = create_task(tmp_path, title="Active task", auto_commit=False)

    running.status = "in_progress"
    running.pipeline_status = "testing"
    running.runtime.execution_status = "running"
    running.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    running.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="running",
        path="subagents/SA-0001-qa",
        pid=5151,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:00:10+00:00",
    )
    save_task(tmp_path, running)
    save_task_runtime(tmp_path, running)

    state = load_state(tmp_path)
    state.active_task_id = running.id
    state.queue = [queued.id]
    save_state(tmp_path, state)
    (tmp_path / ".litehive" / ".runner.lock").write_text(
        yaml.safe_dump({"pid": os.getpid()}, sort_keys=False),
        encoding="utf-8",
    )
    _block_runner_lock(monkeypatch)

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is False
    refreshed = get_task(tmp_path, running.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.active_subagent is not None
    assert load_state(tmp_path).active_task_id == running.id
    assert load_state(tmp_path).queue == [queued.id]


def test_recover_stale_runner_state_persists_cleared_stale_active_marker_without_transition(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending follow-up", auto_commit=False)

    state = load_state(tmp_path)
    state.active_task_id = "T-9999"
    state.queue = [queued.id]
    save_state(tmp_path, state)

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is True
    restored = load_state(tmp_path)
    assert restored.active_task_id is None
    assert restored.queue == [queued.id]


def test_add_command_persists_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Dependent task",
            goal="",
            acceptance_criteria=None,
            depends_on=[first.id, f"{second.id},{first.id}"],
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0003")
    assert task is not None
    assert task.depends_on == [first.id, second.id]
    assert f"depends_on: {first.id}, {second.id}" in output


def test_add_command_persists_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Large task",
            goal="Ship deterministic routing",
            acceptance_criteria=["Document the route", "Block missing retries"],
            depends_on=None,
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.acceptance_criteria == ["Document the route", "Block missing retries"]
    assert "acceptance_criteria: 2" in output
    assert "warning:" not in output


def test_add_command_persists_pm_sizing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Estimated task",
            goal="",
            pm_complexity="moderate",
            planned_effort="m",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            model=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.pm_complexity == "moderate"
    assert task.planned_effort == "m"
    assert "pm_complexity: moderate" in output
    assert "planned_effort: m" in output


def test_add_command_persists_task_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Review queue behavior",
            goal="",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type="review",
            mode=None,
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    brief = (task_dir(tmp_path, task) / "brief.md").read_text(encoding="utf-8")
    assert task.mode == "tasks"
    assert task.task_type == "review"
    assert task.goal == "Review the target change critically and produce an actionable decision with supporting evidence."
    assert "## Template Guidance" in brief
    assert "mode: tasks" in output
    assert "task_type: review" in output


def test_add_command_can_force_implementation_mode_for_typed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Review queue behavior",
            goal="",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type="review",
            mode="implementation",
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.mode == "implementation"
    assert task.task_type == "review"
    assert task.goal == ""
    assert not (task_dir(tmp_path, task) / "brief.md").exists()
    assert "mode: implementation" in output


def test_add_command_warns_when_large_task_lacks_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="Prerequisite")

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Large task",
            goal="Ship deterministic routing",
            acceptance_criteria=None,
            depends_on=[prerequisite.id],
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "acceptance_criteria: 0" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    assert "this task has: dependencies, an explicit goal." in output
    assert "This task will stay in `grooming` until criteria are added." in output
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." in output


def test_add_command_allows_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Active task")
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Waiting behind executor",
            goal="",
            acceptance_criteria=None,
            depends_on=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Created task T-0002" in output
    assert load_state(tmp_path).queue == ["T-0001", "T-0002"]


def test_update_command_replaces_and_clears_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First prerequisite")
    second = create_task(tmp_path, title="Second prerequisite")
    task = create_task(tmp_path, title="Dependent task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=[first.id, f"{second.id},{first.id}"],
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.depends_on == [first.id, second.id]
    assert f"depends_on: {first.id}, {second.id}" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=["none"],
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.depends_on == []
    assert "depends_on: -" in output


def test_update_command_replaces_and_clears_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="Prerequisite")
    task = create_task(
        tmp_path,
        title="Tune task",
        depends_on=[prerequisite.id],
        acceptance_criteria=["Old criterion"],
    )

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=["First criterion", "Second criterion"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.acceptance_criteria == ["First criterion", "Second criterion"]
    assert "acceptance_criteria: 2" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=["none"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.acceptance_criteria == []
    assert "acceptance_criteria: 0" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." in output


def test_update_command_replaces_and_clears_pm_sizing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task", pm_complexity="simple", planned_effort="s")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            model=None,
            retry_limit=None,
            priority=None,
            pm_complexity="complex",
            planned_effort="l",
            goal=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.pm_complexity == "complex"
    assert updated.planned_effort == "l"
    assert "pm_complexity: complex" in output
    assert "planned_effort: l" in output

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            model=None,
            retry_limit=None,
            priority=None,
            pm_complexity="none",
            planned_effort="none",
            goal=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    cleared = get_task(tmp_path, task.id)
    assert cleared is not None
    assert cleared.pm_complexity is None
    assert cleared.planned_effort is None
    assert "pm_complexity: -" in output
    assert "planned_effort: -" in output


def test_update_command_warns_when_metadata_change_makes_task_require_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task")
    queued = get_task(tmp_path, task.id)
    assert queued is not None
    queued.pipeline_status = "testing"
    save_task(tmp_path, queued)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority="high",
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.priority == "high"
    assert updated.pipeline_status == "grooming"
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." in output


def test_update_command_warns_when_goal_makes_task_require_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task")
    queued = get_task(tmp_path, task.id)
    assert queued is not None
    queued.pipeline_status = "testing"
    save_task(tmp_path, queued)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority=None,
            goal="Ship deterministic routing",
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.goal == "Ship deterministic routing"
    assert updated.pipeline_status == "grooming"
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    assert "this task has: an explicit goal." in output
    assert "This task will stay in `grooming` until criteria are added." in output


def test_update_command_reroutes_large_task_missing_criteria_back_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    prerequisite = create_task(tmp_path, title="Prerequisite")
    task = create_task(
        tmp_path,
        title="Tune task",
        depends_on=[prerequisite.id],
        acceptance_criteria=["Existing criterion"],
    )
    queued = get_task(tmp_path, task.id)
    assert queued is not None
    queued.pipeline_status = "testing"
    save_task(tmp_path, queued)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=["none"],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.acceptance_criteria == []
    assert updated.pipeline_status == "grooming"
    assert "acceptance_criteria: 0" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output


def test_update_command_preserves_later_stage_when_acceptance_gate_is_satisfied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Tune task",
        goal="Ship queue CLI",
        acceptance_criteria=["Existing criterion"],
    )
    queued = get_task(tmp_path, task.id)
    assert queued is not None
    queued.pipeline_status = "testing"
    save_task(tmp_path, queued)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            engine=None,
            retry_limit=None,
            priority="high",
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    capsys.readouterr()

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.pipeline_status == "testing"


def test_add_command_rejects_missing_dependency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Blocked task",
            goal="",
            acceptance_criteria=None,
            depends_on=["T-9999"],
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "add failed: Task T-9999 not found" in output


def test_update_command_rejects_dependency_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    first.depends_on = [second.id]
    save_task(tmp_path, first)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=second.id,
            depends_on=[first.id],
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"update failed: Task {second.id} dependency cycle detected via {first.id}" in output


def test_move_command_reorders_queue(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")

    exit_code = _cmd_move(argparse.Namespace(workspace=tmp_path, task_id=third.id, position=1))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [third.id, first.id, second.id]


def test_move_command_reorders_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_move(argparse.Namespace(workspace=tmp_path, task_id=third.id, position=1))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [third.id, first.id, second.id]


def test_add_command_creates_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Pending task",
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            goal="",
            engine=None,
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Created task T-0002" in output
    state = load_state(tmp_path)
    assert state.active_task_id == active.id
    assert state.queue == ["T-0002"]
    queued = get_task(tmp_path, "T-0002")
    assert queued is not None
    assert queued.status == "queued"
    assert queued.pipeline_status == "backlog"


def test_add_command_persists_task_model_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)

    exit_code = _cmd_add(
        argparse.Namespace(
            workspace=tmp_path,
            title="Pending task",
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type=None,
            mode=None,
            goal="",
            engine="gemini",
            model="gemini-2.5-pro",
            retry_limit=None,
            no_auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.engine == "gemini"
    assert task.model == "gemini-2.5-pro"
    assert "model: gemini-2.5-pro" in output


def test_promote_command_moves_queued_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=second.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "position: 1" in output
    assert load_state(tmp_path).queue == [second.id, first.id]


def test_prioritize_command_reorders_future_tasks_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    second = create_task(tmp_path, title="Second task")
    third = create_task(tmp_path, title="Third task")
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_prioritize(
        argparse.Namespace(workspace=tmp_path, task_ids=[third.id, second.id])
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"moved_tasks: {third.id} {second.id}" in output
    assert "moved_count: 2" in output
    assert f"front_of_queue: {third.id} {second.id}" in output
    assert "queue_length: 3" in output
    assert load_state(tmp_path).queue == [third.id, second.id, first.id]


def test_prioritize_command_rejects_duplicate_task_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="First task")

    exit_code = _cmd_prioritize(
        argparse.Namespace(workspace=tmp_path, task_ids=[task.id, task.id])
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"prioritize failed: Task ids must be unique: {task.id}" in output
    assert load_state(tmp_path).queue == [task.id]


def test_prioritize_command_rejects_task_that_is_not_currently_queued(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Pending task")
    not_queued = create_task(tmp_path, title="Not queued task")
    task = get_task(tmp_path, not_queued.id)
    assert task is not None
    task.status = "flagged"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    exit_code = _cmd_prioritize(
        argparse.Namespace(workspace=tmp_path, task_ids=[not_queued.id, queued.id])
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"prioritize failed: Tasks are not queued: {not_queued.id}" in output
    assert load_state(tmp_path).queue == [queued.id]


def test_update_command_allows_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    queued = create_task(tmp_path, title="Pending task")
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=queued.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type=None,
            engine=None,
            retry_limit=None,
            priority="high",
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "priority: high" in output
    updated = get_task(tmp_path, queued.id)
    assert updated is not None
    assert updated.priority == "high"


def test_update_command_rejects_active_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=active.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type=None,
            engine=None,
            retry_limit=None,
            priority="high",
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "update failed: runner is actively using task state that cannot be changed concurrently" in output


def test_promote_command_resumes_flagged_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume me first")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: testing" in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.execution_status == "idle"


def test_promote_command_warns_when_resumed_task_still_needs_acceptance_criteria(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Needs criteria", goal="Ship queue CLI")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.plan = ["Inspect current flow", "Implement gate"]
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: grooming" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." not in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"


def test_promote_command_resumes_flagged_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    flagged = create_task(tmp_path, title="Resume me first")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_promote(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "position: 1" in output
    state = load_state(tmp_path)
    assert state.active_task_id == active.id
    assert state.queue[0] == flagged.id


def test_requeue_command_requeues_flagged_task_to_front(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Needs another pass")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    from litehive.tasks import save_task

    save_task(tmp_path, task)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: implementing" in output
    assert load_state(tmp_path).queue == [flagged.id, first.id]
    requeued = get_task(tmp_path, flagged.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "implementing"


def test_requeue_command_reroutes_large_task_without_acceptance_criteria_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Needs criteria", goal="Ship queue CLI")
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    flagged.status = "flagged"
    flagged.pipeline_status = "testing"
    flagged.plan = ["Inspect current flow", "Implement gate"]
    save_task(tmp_path, flagged)

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    requeued = get_task(tmp_path, flagged.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.pipeline_status == "grooming"


def test_resume_command_preserves_flagged_task_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume later")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    task.runtime.last_outcome.kind = "flagged"
    task.runtime.last_outcome.stage = "accepting"
    task.runtime.last_outcome.reason_code = "verdict_fail"
    task.runtime.last_outcome.reason = "accepting failed"
    save_task(tmp_path, task)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: accepting" in output
    assert load_state(tmp_path).queue == [first.id, flagged.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "accepting"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.last_outcome.kind == "flagged"
    assert resumed.runtime.last_outcome.stage == "accepting"


def test_resume_command_preserves_interrupted_task_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    interrupted = create_task(tmp_path, title="Resume interrupted task")
    task = get_task(tmp_path, interrupted.id)
    assert task is not None
    task.status = "interrupted"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "interrupted"
    task.runtime.last_outcome.kind = "interrupted"
    task.runtime.last_outcome.stage = "testing"
    task.runtime.last_outcome.reason_code = "execution_interrupted"
    task.runtime.last_outcome.reason = "Interrupted run recovered. Resume from `testing`."
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Interrupted run recovered. Resume from `testing`.",
    )
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="interrupted",
        path="subagents/SA-0001-qa",
        pid=4242,
        started_at="2026-04-01T00:00:10+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        transcript_snippet="tests were halfway done",
    )
    save_task(tmp_path, task)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=interrupted.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: testing" in output
    assert load_state(tmp_path).queue == [first.id, interrupted.id]
    resumed = get_task(tmp_path, interrupted.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "testing"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.current_stage.step is None
    assert resumed.runtime.current_stage.status == "idle"
    assert resumed.runtime.last_outcome.kind == "interrupted"
    assert resumed.runtime.last_outcome.stage == "testing"
    assert resumed.runtime.last_outcome.reason_code == "execution_interrupted"
    assert resumed.runtime.last_subagent is not None
    assert resumed.runtime.last_subagent.transcript_snippet == "tests were halfway done"


def test_resume_run_uses_structured_continuation_handoff_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(auto_commit=False))
    create_task(tmp_path, title="Resume with structured handoff", auto_commit=False)
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-01T00:00:00+00:00"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:20+00:00",
    )
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="qa",
        engine="gemini",
        status="running",
        path="subagents/SA-0001-qa",
        pid=5151,
        started_at="2026-04-01T00:00:05+00:00",
        updated_at="2026-04-01T00:00:20+00:00",
        transcript_snippet="tests were halfway done",
        continuation=RuntimeEngineContinuation(session_id="gemini_resume_123"),
    )
    save_task(tmp_path, task)

    tasks_module._prepare_interrupted_task(
        tmp_path,
        task,
        stage="testing",
        summary="Runner stopped mid-testing.",
        reason="Runner stopped mid-testing.",
    )
    save_task(tmp_path, task)
    resumed = tasks_module.resume_task(tmp_path, task.id, front=True)
    assert resumed.runtime.continuation_handoff is not None
    assert resumed.runtime.continuation_handoff.kind == "restart"

    prompts: list[str] = []

    def fake_run(self, current_task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        if current_task.pipeline_status == "testing":
            prompts.append(prompt)
        return _completed_subagent_result(tmp_path, current_task.pipeline_status, engine_name=engine_name)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status in {"done", "queued"}
    assert len(prompts) == 1
    assert "Continuation handoff:" in prompts[0]
    assert "- Kind: restart" in prompts[0]
    assert "- Engine path: gemini -> gemini" in prompts[0]
    assert "- Prior subagent: SA-0001 at `subagents/SA-0001-qa`" in prompts[0]
    assert "- Engine session id: gemini_resume_123" in prompts[0]
    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.runtime.continuation_handoff is None


def test_resume_command_preserves_flagged_commit_to_git_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="First task")
    flagged = create_task(tmp_path, title="Resume commit")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "flagged"
    task.runtime.last_outcome.kind = "flagged"
    task.runtime.last_outcome.stage = "commit_to_git"
    task.runtime.last_outcome.reason_code = "stage_exception"
    task.runtime.last_outcome.reason = "commit failed"
    save_task(tmp_path, task)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: queued" in output
    assert "pipeline_status: commit_to_git" in output
    assert load_state(tmp_path).queue == [first.id, flagged.id]
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "commit_to_git"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.last_outcome.kind == "flagged"
    assert resumed.runtime.last_outcome.stage == "commit_to_git"


def test_resume_command_reroutes_large_task_missing_criteria_from_implementing_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume later", goal="Ship queue CLI")
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    flagged.status = "flagged"
    flagged.pipeline_status = "implementing"
    flagged.runtime.execution_status = "flagged"
    flagged.plan = ["Inspect current flow", "Implement gate"]
    save_task(tmp_path, flagged)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." not in output
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"


def test_resume_command_reroutes_large_task_missing_criteria_from_later_stage_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume later", goal="Ship queue CLI")
    flagged = get_task(tmp_path, task.id)
    assert flagged is not None
    flagged.status = "flagged"
    flagged.pipeline_status = "accepting"
    flagged.runtime.execution_status = "flagged"
    flagged.plan = ["Inspect current flow", "Implement gate"]
    save_task(tmp_path, flagged)

    exit_code = _cmd_resume_task(
        argparse.Namespace(workspace=tmp_path, task_id=flagged.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in output
    resumed = get_task(tmp_path, flagged.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"


def test_requeue_command_requires_flagged_or_cancelled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Still queued")

    exit_code = _cmd_requeue_task(
        argparse.Namespace(workspace=tmp_path, task_id=task.id, front=False)
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not flagged or closed" in output


def test_requeue_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Retry me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        requeue_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "testing"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task requeued for another implementation pass." not in journal


def test_requeue_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Retry me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        requeue_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task requeued for another implementation pass." not in journal


def test_resume_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Resume me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        resume_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "accepting"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task resumed from `accepting`." not in journal


def test_resume_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Resume me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        resume_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "accepting"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task resumed from `accepting`." not in journal


def test_requeue_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Retry me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        requeue_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task requeued for another implementation pass." not in journal


def test_resume_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    flagged = create_task(tmp_path, title="Resume me")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "accepting"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        resume_task(tmp_path, flagged.id, front=True)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.pipeline_status == "accepting"
    assert refreshed.runtime.execution_status == "flagged"
    assert load_state(tmp_path).queue == [queued.id, flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task resumed from `accepting`." not in journal


def test_abandon_command_cancels_task_and_removes_it_from_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    exit_code = _cmd_abandon_task(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: cancelled" in output
    assert "pipeline_status: testing" in output
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == [queued.id]
    abandoned = get_task(tmp_path, flagged.id)
    assert abandoned is not None
    assert abandoned.status == "cancelled"
    assert abandoned.runtime.execution_status == "cancelled"
    journal = (
        tmp_path / ".litehive" / "tasks" / f"{abandoned.id}-{abandoned.slug}" / "journal.md"
    ).read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." in journal


def test_abandon_command_allows_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    flagged = create_task(tmp_path, title="Stop this later")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_abandon_task(argparse.Namespace(workspace=tmp_path, task_id=flagged.id))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: cancelled" in output
    state = load_state(tmp_path)
    assert state.active_task_id == active.id
    assert flagged.id not in state.queue
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "cancelled"


def test_abandon_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    save_task(tmp_path, task)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        abandon_task(tmp_path, flagged.id)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.execution_status != "cancelled"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [flagged.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." not in journal


def test_abandon_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        abandon_task(tmp_path, flagged.id)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.execution_status == "flagged"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [flagged.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." not in journal


def test_abandon_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Stop this task")
    queued = create_task(tmp_path, title="Keep this task")
    task = get_task(tmp_path, flagged.id)
    assert task is not None
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"
    save_task(tmp_path, task)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        abandon_task(tmp_path, flagged.id)

    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.execution_status == "flagged"
    restored_state = load_state(tmp_path)
    assert restored_state.active_task_id is None
    assert restored_state.queue == [flagged.id, queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task abandoned via CLI at stage `testing`." not in journal


def test_stop_command_interrupts_active_task_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stop active task", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at=None,
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict=None,
        summary="",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    exit_code = _cmd_stop_task(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"task: {task.id} {task.title}" in output
    assert "status: interrupted" in output
    assert "pipeline_status: testing" in output
    assert "signal_sent: no" in output
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.pipeline_status == "testing"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `testing` was running." in journal
    assert "Reason: Task stopped via CLI." in journal
    assert "Resume from `testing`." in journal


def test_stop_current_task_requeues_commit_stage_interrupt(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Stop commit task", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at=None,
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict=None,
        summary="",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    summary = stop_current_task(tmp_path)

    assert summary.signal_sent is False
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).active_task_id is None
    assert load_state(tmp_path).queue == [task.id]


def test_stop_current_task_signals_live_runner_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Signal active task", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at=None,
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict=None,
        summary="",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    held_states = iter([True, False])
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr("litehive.tasks._runner_lock_is_held", lambda root: next(held_states, False))
    monkeypatch.setattr(
        "litehive.tasks._read_runner_lock_metadata",
        lambda root: {"pid": 4242, "started_at": "2026-04-01T00:00:00+00:00"},
    )
    monkeypatch.setattr("litehive.tasks._runner_pid_is_alive", lambda pid: True)
    monkeypatch.setattr("litehive.tasks.recover_stale_runner_state", lambda root: False)

    def fake_kill(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr("litehive.tasks.os.kill", fake_kill)

    summary = stop_current_task(tmp_path, wait_timeout_seconds=0.01, poll_interval_seconds=0.01)

    assert signals == [(4242, tasks_module.signal.SIGINT)]
    assert summary.signal_sent is True
    assert summary.runner_pid == 4242
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "interrupted"
    assert refreshed.runtime.execution_status == "interrupted"


def test_runner_flags_task_when_retry_limit_exhausted(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Exhausted task")
    # max_retries=1 allows 1 retry; on the 2nd rejection (rejections > 1) the task is flagged
    task.retry_policy.max_retries = 1
    save_task(tmp_path, task)
    rejection_count = {"n": 0}

    def executor(task, step):  # type: ignore[no-untyped-def]
        if step == "testing":
            rejection_count["n"] += 1
            verdict = "fail"
        else:
            verdict = "pass"
        return StageReport(task_id=task.id, step=step, verdict=verdict, summary=f"{step} {verdict}")

    runner = TaskExecutionRunner(tmp_path, executor, max_retries=1)
    # First run: testing fails → requeued (1 rejection allowed)
    result1 = runner.run(task)
    assert result1.final_status == "queued"
    finish_task_run_transition(tmp_path, task, result1.final_status)
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.runtime.retry_count == 1

    # Second run: testing fails again → retry limit exceeded → flagged
    result2 = runner.run(refreshed)
    assert result2.final_status == "flagged"
    finish_task_run_transition(tmp_path, refreshed, result2.final_status)
    refreshed2 = get_task(tmp_path, task.id)
    assert refreshed2 is not None
    assert refreshed2.runtime.execution_status == "flagged"
    assert refreshed2.status == "flagged"
    assert refreshed2.runtime.retry_count == 2
    assert refreshed2.runtime.retry_limit == 1
    assert refreshed2.runtime.last_outcome.kind == "flagged"
    assert refreshed2.runtime.last_outcome.reason_code == "retry_limit_exhausted"


@pytest.mark.parametrize("outcome", ["wont_do", "deferred", "duplicate"])
def test_close_task_non_implementation_outcomes(tmp_path: Path, outcome: str) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")
    follow_up = create_task(tmp_path, title="Follow up later")
    state = load_state(tmp_path)
    assert task.id in state.queue

    closed = close_task(
        tmp_path,
        task.id,
        outcome=outcome,
        reason="Test reason",
        follow_up_task_id=follow_up.id,
    )

    assert closed.status == outcome
    assert closed.runtime.last_outcome.kind == outcome
    assert closed.runtime.last_outcome.reason_code == outcome
    assert closed.runtime.last_outcome.reason == "Test reason"
    assert closed.runtime.last_outcome.follow_up_task_id == follow_up.id
    state = load_state(tmp_path)
    assert task.id not in state.queue
    journal = (task_dir(tmp_path, closed) / "journal.md").read_text(encoding="utf-8")
    assert f"Task closed: {outcome}." in journal
    assert f"Follow-up task: {follow_up.id}." in journal


def test_close_task_rolls_back_when_atomic_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        close_task(tmp_path, task.id, outcome="deferred", reason="Not now")

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.last_outcome.reason_code is None
    assert load_state(tmp_path).queue == [task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task closed: deferred." not in journal


def test_close_task_rolls_back_when_atomic_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Close me")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        close_task(tmp_path, task.id, outcome="deferred", reason="Not now")

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.last_outcome.reason_code is None
    assert load_state(tmp_path).queue == [task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task closed: deferred." not in journal


def test_cmd_close_task_wont_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Will not implement")
    follow_up = create_task(tmp_path, title="Track replacement")

    exit_code = _cmd_close_task(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            outcome="wont_do",
            reason=None,
            follow_up_task=follow_up.id,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: wont_do" in output
    assert "outcome: wont_do" in output
    assert f"follow_up_task: {follow_up.id}" in output
    closed = get_task(tmp_path, task.id)
    assert closed is not None
    assert closed.status == "wont_do"
    assert closed.runtime.last_outcome.kind == "wont_do"
    assert closed.runtime.last_outcome.reason_code == "wont_do"
    assert closed.runtime.last_outcome.follow_up_task_id == follow_up.id


def test_close_command_allows_future_task_while_runner_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    queued = create_task(tmp_path, title="Won't do later")
    set_active_task(tmp_path, active.id)
    _block_runner_lock(monkeypatch)

    exit_code = _cmd_close_task(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=queued.id,
            outcome="deferred",
            reason=None,
            follow_up_task=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "status: deferred" in output
    assert "outcome: deferred" in output
    state = load_state(tmp_path)
    assert state.active_task_id == active.id
    assert queued.id not in state.queue
    refreshed = get_task(tmp_path, queued.id)
    assert refreshed is not None
    assert refreshed.runtime.last_outcome.reason_code == "deferred"


def test_status_command_shows_explicit_close_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Defer me")
    follow_up = create_task(tmp_path, title="Future reconsideration")

    close_task(
        tmp_path,
        task.id,
        outcome="deferred",
        reason="Revisit after launch",
        follow_up_task_id=follow_up.id,
    )

    _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert f"{task.id} [deferred/backlog]" in output
    assert "outcome=deferred" in output
    assert "reason_code=deferred" in output
    assert f"follow_up_task={follow_up.id}" in output
    assert "reason=Revisit after launch" in output


def test_pool_summary_reports_closed_tasks_with_reason_and_follow_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    closed = create_task(tmp_path, title="Not now")
    follow_up = create_task(tmp_path, title="Revisit later")
    state = load_state(tmp_path)
    state.queue = [task_id for task_id in state.queue if task_id == closed.id]
    save_state(tmp_path, state)

    close_task(
        tmp_path,
        closed.id,
        outcome="deferred",
        reason="Revisit after launch",
        follow_up_task_id=follow_up.id,
    )

    exit_code = _cmd_run(
        argparse.Namespace(
            workspace=tmp_path,
            engine=None,
            model=None,
            drain=False,
            dry_run=False,
            stop_on_failure=None,
            max_tasks=None,
            stop_on_limit=None,
            quota_threshold=None,
            budget_threshold=None,
            pool_usage_cap=None,
            pool_cost_cap=None,
            engine_usage_cap=None,
            engine_budget_cap=None,
            engine_cost=None,
            stop_on_dirty_git=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "closed_tasks: 1" in output
    assert (
        f"closed: {closed.id} Not now status=deferred pipeline_status=backlog "
        f"stage_outcomes=- reason_code=deferred reason=Revisit after launch "
        f"follow_up_task={follow_up.id}"
    ) in output


def test_update_command_updates_task_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="opencode",
            model="zai-coding-plan/glm-5.1",
            retry_limit="2",
            priority="high",
            goal="Ship queue CLI",
            acceptance_criteria=["Task is visible in queue"],
            human_checkpoint=["before_acceptance"],
            task_type="research",
            mode="tasks",
            auto_commit=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "opencode"
    assert updated.model == "zai-coding-plan/glm-5.1"
    assert updated.retry_policy.max_retries == 2
    assert updated.priority == "high"
    assert updated.goal == "Ship queue CLI"
    assert updated.acceptance_criteria == ["Task is visible in queue"]
    assert updated.human_checkpoints == ["before_acceptance"]
    assert updated.task_type == "research"
    assert updated.mode == "tasks"
    assert updated.git.auto_commit is False
    assert "engine: opencode" in output
    assert "model: zai-coding-plan/glm-5.1" in output
    assert "retry_limit: 2" in output
    assert "priority: high" in output
    assert "acceptance_criteria: 1" in output
    assert "human_checkpoints: before_acceptance" in output
    assert "task_type: research" in output


def test_update_command_can_clear_task_model_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task", model="gemini-2.5-pro")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            model="default",
            engine=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.model is None
    assert "model: default" in output


def test_update_command_seeds_template_defaults_when_switching_to_tasks_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Review queue behavior")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type="review",
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode="tasks",
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.mode == "tasks"
    assert updated.task_type == "review"
    assert updated.goal == "Review the target change critically and produce an actionable decision with supporting evidence."
    assert updated.acceptance_criteria
    assert updated.constraints
    assert updated.plan
    assert "acceptance_criteria: 3" in output


def test_update_command_can_clear_task_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune task", task_type="review")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            depends_on=None,
            acceptance_criteria=None,
            human_checkpoint=None,
            task_type="default",
            engine=None,
            retry_limit=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.task_type is None
    assert "task_type: -" in output


def test_update_command_clears_task_retry_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune retry policy", retry_limit=2)

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine=None,
            retry_limit="default",
            acceptance_criteria=None,
            human_checkpoint=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "retry_limit: default" in output
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.retry_policy.max_retries is None


def test_update_command_accepts_gemini_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Gemini task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="gemini",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "gemini"
    assert "engine: gemini" in output


def test_update_command_accepts_copilot_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Tune Copilot task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="copilot",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "copilot"
    assert "engine: copilot" in output


def test_run_all_stops_before_run_when_pre_status_has_explicit_pool_stop_reason(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: T-0001\nqueue:\n  - T-0001\npool_stop_reason: max_tasks_reached\n",
        encoding="utf-8",
    )
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  echo "unexpected run"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    result = subprocess.run(
        ["bash", str(_repo_root() / "scripts" / "run-all.sh"), str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv),
        check=False,
    )

    assert result.returncode == 0
    assert "Pool already stopped: max_tasks_reached" in result.stdout
    assert not run_count_file.exists()

    log_dirs = list((workspace / ".litehive" / "logs" / "run-all").iterdir())
    assert len(log_dirs) == 1
    assert (log_dirs[0] / "0001-pre-status.log").exists()
    assert not (log_dirs[0] / "0001-run.log").exists()


def test_run_all_restarts_litehive_until_queue_is_empty(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue:\n  - T-0001\npool_stop_reason: null\n",
        encoding="utf-8",
    )
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  if [[ "$count" -eq 1 ]]; then
    cat > "{workspace / '.litehive' / 'state.yaml'}" <<'STATE'
active_task_id: null
queue:
  - T-0001
pool_stop_reason: null
STATE
  else
    cat > "{workspace / '.litehive' / 'state.yaml'}" <<'STATE'
active_task_id: null
queue: []
pool_stop_reason: queue_exhausted
STATE
  fi
  echo "tasks_run: 1"
  echo "stop_reason: queue_exhausted"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    result = subprocess.run(
        ["bash", str(_repo_root() / "scripts" / "run-all.sh"), str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv),
        check=False,
    )

    assert result.returncode == 0
    assert "== iteration 1 ==" in result.stdout
    assert "== iteration 2 ==" in result.stdout
    assert "No active or queued tasks remain. Stopping." in result.stdout
    assert run_count_file.read_text(encoding="utf-8").strip() == "2"

    log_dirs = list((workspace / ".litehive" / "logs" / "run-all").iterdir())
    assert len(log_dirs) == 1
    log_dir = log_dirs[0]
    assert (log_dir / "0001-pre-status.log").exists()
    assert (log_dir / "0001-run.log").exists()
    assert (log_dir / "0001-post-status.log").exists()
    assert (log_dir / "0002-pre-status.log").exists()
    assert (log_dir / "0002-run.log").exists()
    assert (log_dir / "0002-post-status.log").exists()


def test_run_all_continues_after_task_requeued(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".litehive").mkdir()
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue:\n  - T-0001\npool_stop_reason: null\n",
        encoding="utf-8",
    )
    counts_dir = tmp_path / "counts"
    counts_dir.mkdir()
    run_count_file = counts_dir / "run-count"

    fake_uv = _write_fake_uv(
        tmp_path,
        f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "python" && "${{3:-}}" == "-" ]]; then
  shift 2
  exec python3 "$@"
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "run" ]]; then
  count="$(cat "{run_count_file}" 2>/dev/null || echo 0)"
  count="$((count + 1))"
  echo "$count" > "{run_count_file}"
  if [[ "$count" -eq 1 ]]; then
    cat > "{workspace / '.litehive' / 'state.yaml'}" <<'STATE'
active_task_id: null
queue:
  - T-0001
pool_stop_reason: null
STATE
    echo "tasks_run: 1"
    echo "stop_reason: task_requeued"
    exit 0
  fi

  cat > "{workspace / '.litehive' / 'state.yaml'}" <<'STATE'
active_task_id: null
queue: []
pool_stop_reason: queue_exhausted
STATE
  echo "tasks_run: 1"
  echo "stop_reason: queue_exhausted"
  exit 0
fi

if [[ "${{1:-}}" == "run" && "${{2:-}}" == "litehive" && "${{3:-}}" == "repair" ]]; then
  echo "repaired: no"
  exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )

    result = subprocess.run(
        ["bash", str(_repo_root() / "scripts" / "run-all.sh"), str(workspace)],
        cwd=_repo_root(),
        text=True,
        capture_output=True,
        env=_with_fake_uv(fake_uv),
        check=False,
    )

    assert result.returncode == 0
    assert "== iteration 1 ==" in result.stdout
    assert "== iteration 2 ==" in result.stdout
    assert "Stopping after litehive reported stop_reason: task_requeued" not in result.stdout
    assert "No active or queued tasks remain. Stopping." in result.stdout
    assert run_count_file.read_text(encoding="utf-8").strip() == "2"

    log_dirs = list((workspace / ".litehive" / "logs" / "run-all").iterdir())
    assert len(log_dirs) == 1
    log_dir = log_dirs[0]
    assert (log_dir / "0001-run.log").exists()
    assert (log_dir / "0001-post-status.log").exists()
    assert (log_dir / "0002-run.log").exists()
    assert (log_dir / "0002-post-status.log").exists()


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _git_status_without_litehive(cwd: Path) -> list[str]:
    status = _run(["git", "status", "--short"], cwd)
    return [line for line in status.splitlines() if line and not line.endswith(".litehive/")]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _with_fake_uv(fake_uv: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_uv.parent}:{env['PATH']}"
    return env


def _write_fake_uv(tmp_path: Path, script: str) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(script, encoding="utf-8")
    fake_uv.chmod(0o755)
    return fake_uv


def _init_git_repo(tmp_path: Path) -> str:
    _run(["git", "init"], tmp_path)
    _run(["git", "config", "user.name", "Litehive Tests"], tmp_path)
    _run(["git", "config", "user.email", "tests@example.com"], tmp_path)
    (tmp_path / "app.txt").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "app.txt"], tmp_path)
    _run(["git", "commit", "-m", "initial"], tmp_path)
    return _run(["git", "rev-parse", "HEAD"], tmp_path)


def _completed_subagent_result(
    tmp_path: Path, step: str, *, engine_name: str = "codex"
) -> SubagentResult:
    worktrees_root = tmp_path / ".litehive" / "worktrees"
    if step == "implementing" and worktrees_root.exists():
        main_app = tmp_path / "app.txt"
        for worktree in sorted(worktrees_root.iterdir()):
            if not worktree.is_dir():
                continue
            worktree_app = worktree / "app.txt"
            if main_app.exists() and worktree_app.exists():
                worktree_app.write_text(main_app.read_text(encoding="utf-8"), encoding="utf-8")
                subprocess.run(["git", "checkout", "--", "app.txt"], cwd=tmp_path, check=True)
                break

    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}",
            role="swe",
            engine=engine_name,
            status="completed",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=tmp_path,
            exit_code=0,
            stdout=(
                "VERDICT: PASS\n"
                f"SUMMARY: {step} complete via {engine_name}\n"
                "FILES_CHANGED:\n"
                "- app.txt\n"
                "TESTS_ADDED: 1\n"
                "TESTS_PASSING: 1\n"
                "WARNINGS:\n"
            ),
            stderr="",
        ),
        transcript="",
        exit_code=0,
    )


def _stage_subagent_result(
    cwd: Path,
    step: str,
    *,
    role: str = "swe",
    engine_name: str = "codex",
    verdict: str = "PASS",
    summary: str | None = None,
    files_changed: list[str] | None = None,
    tests_added: int = 1,
    tests_passing: int = 1,
    warnings: list[str] | None = None,
) -> SubagentResult:
    transcript_lines = [
        f"VERDICT: {verdict}",
        f"SUMMARY: {summary or f'{step} complete via {engine_name}'}",
        "FILES_CHANGED:",
    ]
    for path in files_changed or []:
        transcript_lines.append(f"- {path}")
    transcript_lines.extend(
        [
            f"TESTS_ADDED: {tests_added}",
            f"TESTS_PASSING: {tests_passing}",
            "WARNINGS:",
        ]
    )
    for warning in warnings or []:
        transcript_lines.append(f"- {warning}")
    transcript = "\n".join(transcript_lines)
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}-{engine_name}",
            role=role,
            engine=engine_name,
            status="completed",
            path=f"subagents/{step}-{engine_name}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=cwd,
            exit_code=0,
            stdout=transcript,
            stderr="",
        ),
        transcript=transcript,
        exit_code=0,
    )


def _resource_limited_subagent_result(
    tmp_path: Path,
    step: str,
    *,
    engine_name: str = "codex",
    resource: str = "memory",
    reason: str = "memory limit exceeded (OOM)",
) -> SubagentResult:
    event = ResourceLimitEvent(
        resource=resource,  # type: ignore[arg-type]
        reason=reason,
        observed_signal="oom",
        exit_code=137,
        memory_mb=4096,
        cpu_count=2.0,
        process_limit=256,
    )
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}",
            role="swe",
            engine=engine_name,
            status="failed",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=tmp_path,
            exit_code=137,
            stdout="compiler terminated",
            stderr="OOMKilled: container exceeded memory limit",
        ),
        transcript="[stderr]\nOOMKilled: container exceeded memory limit",
        exit_code=137,
        failure=EngineFailure(
            kind="resource_limit",
            reason=reason,
            classification=resource,
            resource_limit_event=event,
        ),
    )


def _interrupted_subagent_result(
    tmp_path: Path, step: str, *, engine_name: str = "codex"
) -> SubagentResult:
    return SubagentResult(
        ref=SubagentRef(
            id=f"SA-{step}",
            role="swe",
            engine=engine_name,
            status="interrupted",
            path=f"subagents/{step}",
        ),
        execution=CLIExecutionResult(
            adapter=engine_name,
            argv=(engine_name, "exec"),
            cwd=tmp_path,
            exit_code=130,
            stdout="Execution interrupted by user",
            stderr="received SIGINT",
        ),
        transcript="Execution interrupted by user\n\n[stderr]\nreceived SIGINT",
        exit_code=130,
        failure=EngineFailure(
            kind="execution_interrupted",
            reason="execution interrupted",
        ),
    )


def test_run_task_skips_pre_acceptance_hook_when_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="No hook configured", auto_commit=False)
    real_run = subprocess.run

    def fail_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        argv = args[0] if args else kwargs.get("args")
        if list(argv) == ["bash", "-lc", "uv run ruff check litehive tests"]:
            raise AssertionError("pre-acceptance command should not run")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(  # type: ignore[no-untyped-def]
            tmp_path, task.pipeline_status
        ),
    )
    monkeypatch.setattr("litehive.runtime.subprocess.run", fail_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"


def test_stage_report_from_subagent_structures_resource_limit_failures(tmp_path: Path) -> None:
    task = TaskRecord(id="T-0001", slug="native-task", title="Native task")

    report = stage_report_from_subagent(
        task,
        "implementing",
        _resource_limited_subagent_result(tmp_path, "implementing"),
    )

    assert report.verdict == "blocked"
    assert report.summary == "implementing blocked: memory limit exceeded (OOM)"
    assert report.resource_limit_event is not None
    assert report.resource_limit_event.resource == "memory"


def test_run_next_task_records_structured_resource_limit_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Native code task", auto_commit=False)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _resource_limited_subagent_result(  # type: ignore[no-untyped-def]
            tmp_path, "grooming", engine_name=engine_name
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_outcome.reason_code == "resource_limit"
    assert task.runtime.last_outcome.reason == "grooming blocked: memory limit exceeded (OOM)"

    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-native-code-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["outcome_reason_code"] == "resource_limit"
    assert report["resource_limit_event"]["resource"] == "memory"


def test_render_task_summary_includes_resource_limit_signal_and_effective_limits() -> None:
    task = TaskRecord(id="T-0001", slug="native-task", title="Native task")
    task.runtime.last_subagent = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="failed",
        path="subagents/SA-0001-swe",
        sandboxed=True,
        sandbox_summary="sandbox[docker:litehive-external-engine:latest net=none workspace=rw limits=memory=4096m,cpus=2,pids=256]",
        started_at="2026-04-01T10:00:00+00:00",
        updated_at="2026-04-01T10:01:00+00:00",
        completed_at="2026-04-01T10:01:00+00:00",
        exit_code=137,
        transcript_snippet="OOMKilled",
        resource_limit_event=ResourceLimitEvent(
            resource="memory",
            reason="memory limit exceeded (OOM)",
            observed_signal="oom",
            exit_code=137,
            memory_mb=4096,
            cpu_count=2.0,
            process_limit=256,
        ),
    )

    lines = render_task_summary(task, active=False)

    assert any(
        "resource_limit=memory signal=oom exit_code=137 limits=memory_mb=4096,cpu_count=2,process_limit=256"
        in line
        for line in lines
    )


def test_run_task_runs_pre_acceptance_hook_after_testing_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(pre_acceptance_command="uv run ruff check litehive tests"),
    )
    create_task(tmp_path, title="Run ruff before acceptance", auto_commit=False)
    calls: list[str] = []
    real_run = subprocess.run

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        calls.append(task.pipeline_status)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        if list(argv) != ["bash", "-lc", "uv run ruff check litehive tests"]:
            return real_run(argv, cwd=cwd, capture_output=capture_output, text=text, check=check)
        assert list(argv) == ["bash", "-lc", "uv run ruff check litehive tests"]
        assert cwd == tmp_path
        assert capture_output is True
        assert text is True
        assert check is False
        return subprocess.CompletedProcess(argv, 0, stdout="ruff clean\n", stderr="")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls == ["grooming", "implementing", "testing", "accepting"]
    artifact = (
        tmp_path
        / ".litehive"
        / "tasks"
        / "T-0001-run-ruff-before-acceptance"
        / "artifacts"
        / "pre-acceptance-hook.txt"
    )
    assert "command: uv run ruff check litehive tests" in artifact.read_text(encoding="utf-8")
    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-run-ruff-before-acceptance"
            / "reports"
            / "accepting-004.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["hook_results"][0]["point"] == "before_pm_acceptance"
    assert "runner hook passed" in "\n".join(accepting_report["warnings"])


def test_run_task_blocks_before_accepting_when_pre_acceptance_hook_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(pre_acceptance_command="uv run ruff check litehive tests"),
    )
    create_task(tmp_path, title="Block on failing ruff", auto_commit=False)
    calls: list[str] = []

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        calls.append(task.pipeline_status)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="F401 unused import\n")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert calls == ["grooming", "implementing", "testing"]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "accepting"
    assert task.runtime.last_outcome.kind == "blocked"
    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-block-on-failing-ruff"
            / "reports"
            / "accepting-004.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["verdict"] == "blocked"
    assert "accepting blocked by runner hook" in accepting_report["summary"]
    assert accepting_report["hook_results"][0]["point"] == "before_pm_acceptance"
    assert "runner hook failed" in "\n".join(accepting_report["warnings"])


def test_run_task_records_non_blocking_runner_hook_failure_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "before_swe_implementation": [
                    {"command": "echo pre && exit 7", "blocking": False}
                ]
            }
        ),
    )
    create_task(tmp_path, title="Warn on hook failure", auto_commit=False)
    calls: list[str] = []

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        calls.append(task.pipeline_status)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        if list(argv) == ["bash", "-lc", "echo pre && exit 7"]:
            return subprocess.CompletedProcess(argv, 7, stdout="pre\n", stderr="hook warning\n")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls == ["grooming", "implementing", "testing", "accepting"]
    implementing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-warn-on-hook-failure"
            / "reports"
            / "implementing-002.yaml"
        ).read_text(encoding="utf-8")
    )
    assert implementing_report["hook_results"][0]["point"] == "before_swe_implementation"
    assert implementing_report["hook_results"][0]["status"] == "failed"
    assert implementing_report["verdict"] == "pass"
    assert "runner hook failed" in "\n".join(implementing_report["warnings"])


def test_run_task_blocks_when_post_implementation_runner_hook_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "after_swe_implementation": [
                    {"command": "echo post && exit 9", "blocking": True}
                ]
            }
        ),
    )
    create_task(tmp_path, title="Block on post-implementation hook", auto_commit=False)
    calls: list[str] = []

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        calls.append(task.pipeline_status)
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        if list(argv) == ["bash", "-lc", "echo post && exit 9"]:
            return subprocess.CompletedProcess(argv, 9, stdout="post\n", stderr="bad diff\n")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert calls == ["grooming", "implementing"]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "implementing"
    implementing_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-block-on-post-implementation-hook"
            / "reports"
            / "implementing-002.yaml"
        ).read_text(encoding="utf-8")
    )
    assert implementing_report["verdict"] == "blocked"
    assert implementing_report["hook_results"][0]["point"] == "after_swe_implementation"


def test_run_task_runs_after_acceptance_runner_hook_on_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            runner_hooks={
                "after_pm_acceptance": [{"command": "echo accepted", "blocking": True}]
            }
        ),
    )
    create_task(tmp_path, title="Run after acceptance hook", auto_commit=False)

    def fake_subagent_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    def fake_hook(argv, cwd, capture_output, text, check):  # type: ignore[no-untyped-def]
        if list(argv) == ["bash", "-lc", "echo accepted"]:
            return subprocess.CompletedProcess(argv, 0, stdout="accepted\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_subagent_run)
    monkeypatch.setattr("litehive.runtime.subprocess.run", fake_hook)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    accepting_report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-run-after-acceptance-hook"
            / "reports"
            / "accepting-004.yaml"
        ).read_text(encoding="utf-8")
    )
    assert accepting_report["hook_results"][0]["point"] == "after_pm_acceptance"


def _successful_stage_execution(tmp_path: Path, adapter: str, step: str) -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter=adapter,
        argv=(adapter, "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout=(
            "VERDICT: PASS\n"
            f"SUMMARY: {step} complete via {adapter}\n"
            "FILES_CHANGED:\n"
            "- app.txt\n"
            "TESTS_ADDED: 1\n"
            "TESTS_PASSING: 1\n"
            "WARNINGS:\n"
        ),
        stderr="",
    )


def test_classify_execution_limit_matches_codex_usage_limit_transcript() -> None:
    transcript = (
        "[stderr]\n"
        "ERROR: You've hit your usage limit. Upgrade to Pro "
        "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at 5:26 PM."
    )

    assert classify_execution_limit(transcript) == "usage limit reached"


def test_classify_retryable_execution_failure_matches_timeout_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nRequest failed: upstream connection timed out after 30s."
    )

    assert failure is not None
    assert failure.classification == "timeout"
    assert failure.reason == "transient timeout"


def test_classify_retryable_execution_failure_matches_codex_network_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nError: error sending request for url (https://chatgpt.com/backend-api/codex): connection closed unexpectedly"
    )

    assert failure is not None
    assert failure.classification == "network"
    assert failure.reason == "transient network failure"


def test_classify_retryable_execution_failure_matches_opencode_network_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nOpenCode error: fetch failed: getaddrinfo ENOTFOUND api.z.ai"
    )

    assert failure is not None
    assert failure.classification == "network"
    assert failure.reason == "transient network failure"


def test_classify_retryable_execution_failure_matches_opencode_service_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nOpenCode error: request failed with status code 503 Service Temporarily Unavailable"
    )

    assert failure is not None
    assert failure.classification == "service"
    assert failure.reason == "transient service failure"


def test_classify_retryable_execution_failure_matches_gemini_timeout_transcript() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"message":"Request failed: read timeout while waiting for generativelanguage.googleapis.com"}}'
    )

    assert failure is not None
    assert failure.classification == "timeout"
    assert failure.reason == "transient timeout"


def test_classify_retryable_execution_failure_matches_gemini_network_transcript() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"message":"Client network socket disconnected before secure TLS connection was established"}}'
    )

    assert failure is not None
    assert failure.classification == "network"
    assert failure.reason == "transient network failure"


def test_classify_retryable_execution_failure_matches_gemini_service_transcript() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"message":"GoogleGenerativeAI Error: [503 Service Unavailable] backend unavailable, try again later"}}'
    )

    assert failure is not None
    assert failure.classification == "service"
    assert failure.reason == "transient service failure"


def test_classify_retryable_execution_failure_matches_claude_timeout_payload() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"type":"request_timeout","message":"Request timed out while waiting for Anthropic response"}}'
    )

    assert failure is not None
    assert failure.classification == "timeout"
    assert failure.reason == "transient timeout"


def test_classify_retryable_execution_failure_matches_claude_network_payload() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"message":"Network connection was lost before the response completed"}}'
    )

    assert failure is not None
    assert failure.classification == "network"
    assert failure.reason == "transient network failure"


def test_classify_retryable_execution_failure_matches_claude_service_payload() -> None:
    failure = classify_retryable_execution_failure(
        '[stderr]\n{"type":"error","error":{"type":"overloaded_error","message":"Anthropic\'s systems are overloaded. Please try again later."}}'
    )

    assert failure is not None
    assert failure.classification == "service"
    assert failure.reason == "transient service failure"


def test_classify_retryable_execution_failure_skips_codex_usage_limit_transcript() -> None:
    failure = classify_retryable_execution_failure(
        "[stderr]\nERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits."
    )

    assert failure is None


def test_classify_retryable_execution_failure_skips_opencode_rate_limit_transcript() -> None:
    transcript = "[stderr]\nOpenCode error: 429 Too Many Requests: rate limit exceeded"

    assert classify_execution_limit(transcript) == "rate limit reached"
    assert classify_retryable_execution_failure(transcript) is None


def test_classify_retryable_execution_failure_skips_gemini_quota_limit_transcript() -> None:
    transcript = (
        '[stderr]\n{"type":"error","error":{"message":"GoogleGenerativeAI Error: [429 Too Many Requests] '
        'You exceeded your current quota, please check your plan and billing details."}}'
    )

    assert classify_execution_limit(transcript) == "quota limit reached"
    assert classify_retryable_execution_failure(transcript) is None


def test_classify_retryable_execution_failure_skips_claude_rate_limit_payload() -> None:
    transcript = (
        '[stderr]\n{"type":"error","error":{"type":"rate_limit_error","message":"Your account has hit a rate limit. '
        'Please slow down and try again later."}}'
    )

    assert classify_execution_limit(transcript) == "rate limit reached"
    assert classify_retryable_execution_failure(transcript) is None


def test_classify_retryable_execution_failure_skips_claude_spend_limit_payload() -> None:
    transcript = (
        '[stderr]\n{"type":"error","error":{"message":"Monthly spend limit reached for this workspace budget."}}'
    )

    assert classify_execution_limit(transcript) == "budget limit reached"
    assert classify_retryable_execution_failure(transcript) is None


def test_run_next_task_uses_routing_plan_before_global_fallbacks_when_budget_blocks_first_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="codex"))
    create_task(tmp_path, title="Research engine quota behavior", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        assert engine_name == "codex"
        return _completed_subagent_result(tmp_path, task.pipeline_status)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_task(
        tmp_path,
        require_task(tmp_path, "T-0001"),
        budget_ledger=EngineBudgetLedger(engine_usage_caps={"gemini": 0}),
    )

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "gemini"
    assert task.runtime.last_engine_switch.to_engine == "codex"
    assert "engine usage cap reached for `gemini`" in task.runtime.last_engine_switch.reason


def test_run_next_task_falls_back_from_codex_to_opencode_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fallback usage-limit task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr("litehive.subagents._supports_live_execution", lambda engine: False)

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr=(
                    "ERROR: You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
                    "to purchase more credits or try again at 5:26 PM."
                ),
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    assert task.runtime.last_engine_switch.reason == "usage limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-fallback-usage-limit-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached."
        in report["warnings"]
    )
    assert report["feedback"].startswith(
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached."
    )
    assert "SUMMARY: grooming complete via opencode" in report["feedback"]
    _cmd_status(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out
    assert "engine_switch=grooming codex->opencode reason=usage limit reached" in output


def test_run_next_task_falls_back_from_codex_to_gemini_on_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={
                "codex": ["gemini"],
                "opencode": ["codex", "gemini", "copilot"],
                "gemini": ["codex", "opencode", "copilot"],
                "copilot": ["codex", "opencode", "gemini"],
            }
        ),
    )
    create_task(tmp_path, title="Gemini fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    gemini = get_engine("gemini")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: True)
    monkeypatch.setattr("litehive.subagents._supports_live_execution", lambda engine: False)

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later or purchase more credits.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_gemini_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        transcript = (
            '{"type":"message","role":"assistant","content":"VERDICT: PASS\\n","delta":true}\n'
            f'{{"type":"message","role":"assistant","content":"SUMMARY: {step} complete via gemini\\nFILES_CHANGED:\\n- app.txt\\nTESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n","delta":true}}'
        )
        return CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=cwd,
            exit_code=0,
            stdout=transcript,
            stderr="",
        )

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(gemini, "run", fake_gemini_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "gemini"
    assert task.runtime.last_engine_switch.reason == "usage limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-gemini-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `gemini` after usage limit reached."
        in report["warnings"]
    )


def test_run_next_task_keeps_using_fallback_engine_after_implementing_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task", engine="codex", auto_commit=False)
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    save_task(tmp_path, task)

    codex = get_engine("codex")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr("litehive.subagents._supports_live_execution", lambda engine: False)

    attempted_stages: list[tuple[str, str]] = []

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        attempted_stages.append(("codex", step))
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
        )

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        attempted_stages.append(("opencode", step))
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempted_stages == [
        ("codex", "implementing"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.step == "implementing"
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"


def test_run_next_task_walks_same_stage_fallback_graph_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={
                "codex": ["opencode"],
                "opencode": ["gemini"],
                "gemini": ["copilot"],
                "copilot": [],
            }
        ),
    )
    create_task(tmp_path, title="Chained fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    opencode = get_engine("opencode")
    gemini = get_engine("gemini")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: True)
    monkeypatch.setattr("litehive.subagents._supports_live_execution", lambda engine: False)

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=1,
                stdout="rate limit exceeded",
                stderr="",
            )
        return _successful_stage_execution(tmp_path, "opencode", "non-grooming")

    def fake_gemini_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        transcript = (
            '{"type":"message","role":"assistant","content":"VERDICT: PASS\\n","delta":true}\n'
            f'{{"type":"message","role":"assistant","content":"SUMMARY: {step} complete via gemini\\nFILES_CHANGED:\\n- app.txt\\nTESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n","delta":true}}'
        )
        return CLIExecutionResult(
            adapter="gemini",
            argv=("gemini", "-p"),
            cwd=cwd,
            exit_code=0,
            stdout=transcript,
            stderr="",
        )

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)
    monkeypatch.setattr(gemini, "run", fake_gemini_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "opencode"
    assert task.runtime.last_engine_switch.to_engine == "gemini"
    assert task.runtime.last_engine_switch.reason == "rate limit reached"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-chained-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"][:2] == [
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached.",
        "Stage `grooming` switched from `opencode` to `gemini` after rate limit reached.",
    ]
    assert report["feedback"].startswith(report["warnings"][0])
    assert "SUMMARY: grooming complete via gemini" in report["feedback"]


def test_run_next_task_retries_retryable_execution_failure_before_continuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            opencode_model="zai-coding-plan/glm-5.1",
            execution_retry_policies={
                "model_family:glm": {
                    "max_retries": 2,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            }
        ),
    )
    create_task(tmp_path, title="Retry transient engine failure", engine="opencode", auto_commit=False)

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    grooming_attempts = 0

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if step == "grooming":
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "exec"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout="",
                        stderr="request timed out",
                    ),
                    transcript="[stderr]\nrequest timed out",
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient timeout",
                        classification="timeout",
                    ),
                )
        return _completed_subagent_result(tmp_path, step)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("opencode", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is None
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-retry-transient-engine-failure"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` retrying `opencode` after attempt 1/3 due to transient timeout "
        "(classification: timeout, policy: opencode, backoff: 0.25s)."
        in report["warnings"]
    )


def test_run_next_task_reuses_structured_continuation_handoff_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            opencode_model="zai-coding-plan/glm-5.1",
            execution_retry_policies={
                "model_family:glm": {
                    "max_retries": 1,
                    "backoff_seconds": 0.0,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            },
        ),
    )
    create_task(tmp_path, title="Retry with continuation handoff", engine="opencode", auto_commit=False)

    prompts: list[str] = []
    grooming_attempts = 0

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        if step == "grooming":
            prompts.append(prompt)
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "run"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout="\n".join(
                            [
                                '{"type":"step_start","timestamp":1,"sessionID":"ses_retry","part":{"id":"prt_1","type":"step-start"}}',
                                '{"type":"text","timestamp":2,"sessionID":"ses_retry","part":{"id":"prt_2","type":"text","text":"Halfway through grooming"}}',
                            ]
                        ),
                        stderr="request timed out",
                    ),
                    transcript="Halfway through grooming\n\n[stderr]\nrequest timed out",
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient timeout",
                        classification="timeout",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert len(prompts) == 2
    assert "Continuation handoff:" not in prompts[0]
    assert "Continuation handoff:" in prompts[1]
    assert "- Kind: retry" in prompts[1]
    assert "- Reason: transient timeout" in prompts[1]
    assert "- Prior subagent: SA-grooming-1 at `subagents/grooming-1`" in prompts[1]
    assert "- Engine session id: ses_retry" in prompts[1]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.continuation_handoff is None


def test_run_next_task_passes_structured_continuation_handoff_across_engine_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(engine_fallbacks={"codex": ["opencode"]}),
    )
    create_task(tmp_path, title="Engine switch with continuation handoff", engine="codex", auto_commit=False)

    prompts_by_engine: list[tuple[str, str]] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        step = task.pipeline_status
        if step == "grooming":
            prompts_by_engine.append((engine_name, prompt))
        if engine_name == "codex" and step == "grooming":
            return SubagentResult(
                ref=SubagentRef(
                    id="SA-grooming-codex",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path="subagents/grooming-codex",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                        stdout="\n".join(
                            [
                                '{"type":"thread.started","thread_id":"thread_codex_123"}',
                                "{\"type\":\"error\",\"message\":\"You've hit your usage limit\"}",
                            ]
                        ),
                    stderr="",
                ),
                transcript="You've hit your usage limit",
                exit_code=1,
                failure=EngineFailure(kind="execution_limit", reason="usage limit reached"),
            )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert [engine for engine, _prompt in prompts_by_engine[:2]] == ["codex", "opencode"]
    opencode_prompt = prompts_by_engine[1][1]
    assert "Continuation handoff:" in opencode_prompt
    assert "- Kind: engine_switch" in opencode_prompt
    assert "- Reason: usage limit reached" in opencode_prompt
    assert "- Engine path: codex -> opencode" in opencode_prompt
    assert "- Engine thread id: thread_codex_123" in opencode_prompt
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.continuation_handoff is None


def test_run_next_task_uses_default_opencode_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    create_task(tmp_path, title="Retry transient opencode network failure", engine="opencode", auto_commit=False)

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    grooming_attempts = 0

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if step == "grooming":
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "run"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout="",
                        stderr="OpenCode error: fetch failed: getaddrinfo ENOTFOUND api.z.ai",
                    ),
                    transcript="[stderr]\nOpenCode error: fetch failed: getaddrinfo ENOTFOUND api.z.ai",
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient network failure",
                        classification="network",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("opencode", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-retry-transient-opencode-network-failure"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    expected_warning = (
        "Stage `grooming` retrying `opencode` after attempt 1/3 due to transient network failure "
        "(classification: network, policy: opencode, backoff: 0.25s)."
    )
    assert expected_warning in report["warnings"]
    journal = (
        tmp_path
        / ".litehive"
        / "tasks"
        / "T-0001-retry-transient-opencode-network-failure"
        / "journal.md"
    ).read_text(encoding="utf-8")
    assert expected_warning in journal


def test_run_next_task_uses_default_gemini_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig())
    create_task(tmp_path, title="Retry transient gemini network failure", engine="gemini", auto_commit=False)

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    grooming_attempts = 0

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if step == "grooming":
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "-p"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout="",
                        stderr=(
                            '{"type":"error","error":{"message":"Client network socket disconnected '
                            'before secure TLS connection was established"}}'
                        ),
                    ),
                    transcript=(
                        '[stderr]\n{"type":"error","error":{"message":"Client network socket '
                        'disconnected before secure TLS connection was established"}}'
                    ),
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient network failure",
                        classification="network",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("gemini", "grooming"),
        ("gemini", "grooming"),
        ("gemini", "implementing"),
        ("gemini", "testing"),
        ("gemini", "accepting"),
    ]
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-retry-transient-gemini-network-failure"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    expected_warning = (
        "Stage `grooming` retrying `gemini` after attempt 1/3 due to transient network failure "
        "(classification: network, policy: gemini, backoff: 0.25s)."
    )
    assert expected_warning in report["warnings"]
    journal = (
        tmp_path
        / ".litehive"
        / "tasks"
        / "T-0001-retry-transient-gemini-network-failure"
        / "journal.md"
    ).read_text(encoding="utf-8")
    assert expected_warning in journal


def test_run_next_task_uses_default_claude_retry_policy_and_records_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    create_task(tmp_path, title="Retry transient claude service failure", engine="claude", auto_commit=False)

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    grooming_attempts = 0

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        nonlocal grooming_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if step == "grooming":
            grooming_attempts += 1
            if grooming_attempts == 1:
                return SubagentResult(
                    ref=SubagentRef(
                        id="SA-grooming-1",
                        role=role,
                        engine=engine_name,
                        status="failed",
                        path="subagents/grooming-1",
                    ),
                    execution=CLIExecutionResult(
                        adapter=engine_name,
                        argv=(engine_name, "-p"),
                        cwd=tmp_path,
                        exit_code=1,
                        stdout='{"type":"error","error":{"type":"overloaded_error","message":"Anthropic systems are overloaded, please try again later"}}',
                        stderr="",
                    ),
                    transcript=(
                        '[stderr]\n{"type":"error","error":{"type":"overloaded_error",'
                        '"message":"Anthropic systems are overloaded, please try again later"}}'
                    ),
                    exit_code=1,
                    failure=EngineFailure(
                        kind="retryable_execution_error",
                        reason="transient service failure",
                        classification="service",
                    ),
                )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("claude", "grooming"),
        ("claude", "grooming"),
        ("claude", "implementing"),
        ("claude", "testing"),
        ("claude", "accepting"),
    ]
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-retry-transient-claude-service-failure"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    expected_warning = (
        "Stage `grooming` retrying `claude` after attempt 1/3 due to transient service failure "
        "(classification: service, policy: claude, backoff: 0.25s)."
    )
    assert expected_warning in report["warnings"]
    journal = (
        tmp_path
        / ".litehive"
        / "tasks"
        / "T-0001-retry-transient-claude-service-failure"
        / "journal.md"
    ).read_text(encoding="utf-8")
    assert expected_warning in journal


def test_run_next_task_uses_codex_retry_policy_before_external_cli_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={"codex": ["opencode"]},
            execution_retry_policies={
                "codex": {
                    "max_retries": 1,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["network"],
                },
                "external_cli": {
                    "max_retries": 5,
                    "backoff_seconds": 9.0,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                },
            },
        ),
    )
    create_task(tmp_path, title="Codex retry policy task", engine="codex", auto_commit=False)

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []
    codex_attempts = 0

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        nonlocal codex_attempts
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if engine_name == "opencode":
            return _completed_subagent_result(tmp_path, step, engine_name="opencode")
        codex_attempts += 1
        if codex_attempts <= 2:
            return SubagentResult(
                ref=SubagentRef(
                    id=f"SA-{step}-{codex_attempts}",
                    role=role,
                    engine=engine_name,
                    status="failed",
                    path=f"subagents/{step}-{codex_attempts}",
                ),
                execution=CLIExecutionResult(
                    adapter=engine_name,
                    argv=(engine_name, "exec"),
                    cwd=tmp_path,
                    exit_code=1,
                    stdout="",
                    stderr="error sending request: connection closed",
                ),
                transcript="[stderr]\nerror sending request: connection closed",
                exit_code=1,
                failure=EngineFailure(
                    kind="retryable_execution_error",
                    reason="transient network failure",
                    classification="network",
                ),
            )
        return _completed_subagent_result(tmp_path, step, engine_name=engine_name)

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("codex", "grooming"),
        ("codex", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    assert task.runtime.last_engine_switch.reason == "transient network failure"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-codex-retry-policy-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"][:3] == [
        "Stage `grooming` retrying `codex` after attempt 1/2 due to transient network failure (classification: network, policy: codex, backoff: 0.25s).",
        "Stage `grooming` stopped retrying `codex` after attempt 2/2: transient network failure.",
        "Stage `grooming` switched from `codex` to `opencode` after transient network failure.",
    ]


def test_run_next_task_falls_back_after_retry_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={"codex": ["opencode"]},
            execution_retry_policies={
                "external_cli": {
                    "max_retries": 2,
                    "backoff_seconds": 0.1,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            },
        ),
    )
    create_task(tmp_path, title="Fallback after transient retries", engine="codex", auto_commit=False)

    monkeypatch.setattr("litehive.runtime.time.sleep", lambda _seconds: None)

    attempts: list[tuple[str, str]] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if engine_name == "opencode":
            return _completed_subagent_result(tmp_path, step, engine_name="opencode")
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{step}-{len(attempts)}",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{step}-{len(attempts)}",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="",
                stderr="request timed out",
            ),
            transcript="[stderr]\nrequest timed out",
            exit_code=1,
            failure=EngineFailure(
                kind="retryable_execution_error",
                reason="transient timeout",
                classification="timeout",
            ),
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("codex", "grooming"),
        ("codex", "grooming"),
        ("codex", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "codex"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    assert task.runtime.last_engine_switch.reason == "transient timeout"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-fallback-after-transient-retries"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"][:3] == [
        "Stage `grooming` retrying `codex` after attempt 1/3 due to transient timeout (classification: timeout, policy: codex, backoff: 0.25s).",
        "Stage `grooming` retrying `codex` after attempt 2/3 due to transient timeout (classification: timeout, policy: codex, backoff: 0.50s).",
        "Stage `grooming` stopped retrying `codex` after attempt 3/3: transient timeout.",
    ]
    assert (
        "Stage `grooming` switched from `codex` to `opencode` after transient timeout."
        in report["warnings"]
    )
    assert report["feedback"].startswith(report["warnings"][0])


def test_run_next_task_does_not_retry_codex_usage_limit_when_codex_policy_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={"codex": ["opencode"]},
            execution_retry_policies={
                "codex": {
                    "max_retries": 2,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout", "network", "service"],
                }
            },
        ),
    )
    create_task(tmp_path, title="Codex usage limit is not retryable", engine="codex", auto_commit=False)

    attempts: list[tuple[str, str]] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        step = task.pipeline_status
        attempts.append((engine_name, step))
        if engine_name == "opencode":
            return _completed_subagent_result(tmp_path, step, engine_name="opencode")
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{step}-1",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{step}-1",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits.",
            ),
            transcript=(
                "[stderr]\nERROR: You've hit your usage limit. Visit "
                "https://chatgpt.com/codex/settings/usage to purchase more credits."
            ),
            exit_code=1,
            failure=EngineFailure(kind="execution_limit", reason="usage limit reached"),
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert attempts == [
        ("codex", "grooming"),
        ("opencode", "grooming"),
        ("opencode", "implementing"),
        ("opencode", "testing"),
        ("opencode", "accepting"),
    ]
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-codex-usage-limit-is-not-retryable"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["warnings"] == [
        "Stage `grooming` switched from `codex` to `opencode` after usage limit reached."
    ]


def test_run_next_task_skips_retries_for_non_retryable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            execution_retry_policies={
                "external_cli": {
                    "max_retries": 2,
                    "backoff_seconds": 0.25,
                    "backoff_multiplier": 2.0,
                    "retry_on": ["timeout"],
                }
            }
        ),
    )
    create_task(tmp_path, title="Skip non-retryable failure", engine="codex", auto_commit=False)

    attempts: list[tuple[str, str]] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        step = task.pipeline_status
        attempts.append((engine_name, step))
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{step}-1",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{step}-1",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="",
                stderr="fatal parse error",
            ),
            transcript="[stderr]\nfatal parse error",
            exit_code=1,
            failure=None,
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    assert attempts == [("codex", "grooming")]


def test_run_next_task_skips_unavailable_fallback_engine_after_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            engine_fallbacks={
                "codex": ["gemini", "opencode"],
                "opencode": ["codex", "gemini", "copilot"],
                "gemini": ["codex", "opencode", "copilot"],
                "copilot": ["codex", "opencode", "gemini"],
            }
        ),
    )
    create_task(tmp_path, title="Unavailable fallback task", engine="codex", auto_commit=False)
    codex = get_engine("codex")
    gemini = get_engine("gemini")
    opencode = get_engine("opencode")

    monkeypatch.setattr(codex, "is_available", lambda: True)
    monkeypatch.setattr(gemini, "is_available", lambda: False)
    monkeypatch.setattr(opencode, "is_available", lambda: True)
    monkeypatch.setattr("litehive.subagents._supports_live_execution", lambda engine: False)

    def fake_codex_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        if "Stage: grooming" in prompt:
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=1,
                stdout="",
                stderr="ERROR: You've hit your usage limit. Try again later.",
            )
        return _successful_stage_execution(tmp_path, "codex", "non-grooming")

    def fake_opencode_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        on_started=None,
    ) -> CLIExecutionResult:
        step = prompt.split("Stage: ", 1)[1].splitlines()[0]
        return _successful_stage_execution(tmp_path, "opencode", step)

    monkeypatch.setattr(codex, "run", fake_codex_run)
    monkeypatch.setattr(opencode, "run", fake_opencode_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_engine_switch is not None
    assert task.runtime.last_engine_switch.from_engine == "gemini"
    assert task.runtime.last_engine_switch.to_engine == "opencode"
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-unavailable-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        "Stage `grooming` switched from `codex` to `gemini` after usage limit reached."
        in report["warnings"]
    )
    assert (
        "Stage `grooming` switched from `gemini` to `opencode` after Engine 'gemini' is unavailable: missing binary 'gemini'."
        in report["warnings"]
    )


def test_run_next_task_creates_checkpoint_commit_and_persists_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is not None
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 ship-checkpoint"
    )

    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_message == "litehive: complete T-0001 ship-checkpoint"
    assert task.git.commit_sha == summary.commit_sha
    assert task.git.checkpoint_attempts == 1
    assert task.git.checkpoint_base_sha == initial_sha
    assert task.git.rolled_back_checkpoint_attempt is None
    assert task.runtime.execution_status == "done"
    assert task.runtime.last_stage.step == "commit_to_git"
    assert task.runtime.last_stage.verdict == "pass"
    assert task.runtime.git.commit_sha == summary.commit_sha
    assert task.git.worktree_path is None
    assert not (tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}").exists()


def test_run_next_task_executes_stage_in_task_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Run in worktree", auto_commit=False)
    seen_execution_roots: list[Path] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        seen_execution_roots.append(self.execution_root)
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name)
        if task.pipeline_status == "implementing":
            assert self.execution_root != tmp_path
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            (self.execution_root / "app.txt").write_text("worktree-only\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert seen_execution_roots
    assert all(path != tmp_path for path in seen_execution_roots)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"


def test_run_next_task_keeps_using_task_worktree_when_main_checkout_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Run in isolated worktree", auto_commit=False)
    (tmp_path / "README.md").write_text("main checkout dirt\n", encoding="utf-8")
    seen_execution_roots: list[Path] = []

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        seen_execution_roots.append(self.execution_root)
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name)
        if task.pipeline_status == "implementing":
            assert self.execution_root != tmp_path
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            (self.execution_root / "app.txt").write_text("worktree-only\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert seen_execution_roots
    assert all(path != tmp_path for path in seen_execution_roots)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "main checkout dirt\n"


def test_run_next_task_cherry_picks_task_commit_back_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Cherry-pick worktree commit")

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        result = _completed_subagent_result(tmp_path, task.pipeline_status, engine_name=engine_name)
        if task.pipeline_status == "implementing":
            assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
            (self.execution_root / "app.txt").write_text("integrated\n", encoding="utf-8")
        return result

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "integrated\n"


def test_checkpoint_message_attempt_policy_matches_generated_subjects_only(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Message policy", auto_commit=False)

    assert checkpoint_message(task, attempt=1) == "litehive: complete T-0001 message-policy"
    assert checkpoint_message(task, attempt=2) == "litehive: complete T-0001 message-policy (attempt 2)"

    task.git.commit_message = "custom: keep subject"
    assert checkpoint_message(task, attempt=2) == "custom: keep subject"

    task.git.commit_message = "litehive: checkpoint T-0001 message-policy"
    assert checkpoint_message(task, attempt=2) == "litehive: checkpoint T-0001 message-policy (attempt 2)"


def test_run_next_task_appends_attempt_suffix_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated-once\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    first = run_next_task(tmp_path)
    assert first.result is not None
    assert first.result.final_status == "done"

    rollback_completed_task(tmp_path, "T-0001")
    assert _git_status_without_litehive(tmp_path) == []

    (tmp_path / "app.txt").write_text("updated-twice\n", encoding="utf-8")
    second = run_next_task(tmp_path)

    assert second.result is not None
    assert second.result.final_status == "done"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 ship-checkpoint (attempt 2)"
    )
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.checkpoint_attempts == 2
    assert task.git.rolled_back_checkpoint_attempt is None


def test_run_next_task_preserves_future_task_added_during_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Ship checkpoint")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    def fail_commit_with_concurrent_add(
        root: Path, message: str, *, paths: list[str] | None = None
    ) -> None:
        create_task(tmp_path, title="Added during commit failure", auto_commit=False)
        raise GitError("simulated commit failure")

    monkeypatch.setattr("litehive.runtime.commit_task", fail_commit_with_concurrent_add)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    state = load_state(tmp_path)
    assert state.active_task_id is None
    assert state.queue == ["T-0002"]
    added = get_task(tmp_path, "T-0002")
    assert added is not None
    assert added.title == "Added during commit failure"
    assert added.status == "queued"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.pipeline_status == "commit_to_git"
    assert task.git.checkpoint_attempts == 0


def test_run_next_task_flags_task_when_commit_stage_prerequisite_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Needs git repo")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "flagged"
    assert task.runtime.last_outcome.kind == "flagged"
    assert task.runtime.last_outcome.reason_code == "verdict_fail"
    assert task.runtime.last_outcome.retry_limit == 3
    assert task.pipeline_status == "commit_to_git"
    assert task.git.commit_sha is None


def test_run_next_task_records_blocked_reason_code_when_fallbacks_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Exhausted fallback task", engine="codex", auto_commit=False)

    def fake_run(self, task, role, engine_name, prompt, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id=f"SA-{role}-{engine_name}",
                role=role,
                engine=engine_name,
                status="failed",
                path=f"subagents/{role}-{engine_name}",
            ),
            execution=CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "exec"),
                cwd=tmp_path,
                exit_code=1,
                stdout="quota exceeded",
                stderr="",
            ),
            transcript="quota exceeded",
            exit_code=1,
            failure=EngineFailure(kind="execution_limit", reason="quota exceeded"),
        )

    monkeypatch.setattr("litehive.runtime.SubagentManager.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "flagged"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.runtime.last_outcome.kind == "blocked"
    assert task.runtime.last_outcome.reason_code == "verdict_blocked"
    assert task.runtime.last_outcome.retry_limit == 3
    report = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-exhausted-fallback-task"
            / "reports"
            / "grooming-001.yaml"
        ).read_text(encoding="utf-8")
    )
    assert report["outcome"] == "blocked"
    assert report["outcome_reason_code"] == "verdict_blocked"


def test_run_next_task_skips_commit_stage_when_auto_commit_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Skip commit", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_sha is None
    assert task.runtime.git.commit_sha is None


def test_run_next_task_skips_commit_stage_when_workspace_auto_commit_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path, LitehiveConfig(auto_commit=False))
    create_task(tmp_path, title="Skip commit from config")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.git.commit_sha is None
    assert task.runtime.git.commit_sha is None


def test_run_next_task_flags_task_when_repo_has_unrelated_dirty_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Dirty repo should block commit")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("unrelated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "done"
    assert task.pipeline_status == "done"


def test_run_next_task_flags_task_when_other_task_state_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    first = create_task(tmp_path, title="Ship first task")
    create_task(tmp_path, title="Unrelated pending task")
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.task.id == first.id
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert summary.commit_sha is not None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "litehive: complete T-0001 ship-first-task"
    task = get_task(tmp_path, first.id)
    assert task is not None
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None
    task_yaml = yaml.safe_load(
        (
            tmp_path
            / ".litehive"
            / "tasks"
            / "T-0001-ship-first-task"
            / "task.yaml"
        ).read_text(encoding="utf-8")
    )
    assert task_yaml["git"]["commit_sha"] == task.git.commit_sha


def test_rollback_command_requeues_checkpointed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Fix after done")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_rollback(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    rollback_output = capsys.readouterr().out

    assert exit_code == 0
    assert "rollback_commit:" in rollback_output
    assert "recovery_policy: rollback reverted the checkpoint and requeued the task" in rollback_output
    assert "next_commit_message: litehive: complete T-0001 fix-after-done (attempt 2)" in rollback_output
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: rollback T-0001 fix-after-done (attempt 1)"
    )
    task = get_task(tmp_path, "T-0001")
    assert task is not None
    assert task.status == "queued"
    assert task.pipeline_status == "implementing"
    assert task.git.rolled_back_checkpoint_attempt == 1
    assert load_state(tmp_path).queue == ["T-0001"]


def test_recover_command_requeues_completed_task_without_revert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Recover without revert")
    (tmp_path / "app.txt").write_text("ship-again\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "task: T-0001 Recover without revert" in recover_output
    assert "pipeline_status: implementing" in recover_output
    assert "recovery_policy: recover requeued the task without reverting workspace code" in recover_output
    assert "next_commit_message: litehive: complete T-0001 recover-without-revert (attempt 2)" in recover_output
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "ship-again\n"
    assert load_state(tmp_path).queue == ["T-0001"]


def test_recover_completed_task_clears_checkpoint_pointer_and_next_run_uses_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Recover rerun")
    (tmp_path / "app.txt").write_text("first-pass\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    first = run_next_task(tmp_path)
    assert first.result is not None
    assert first.result.final_status == "done"

    recovered = recover_completed_task(tmp_path, "T-0001")
    assert recovered.git.commit_sha is None
    assert recovered.runtime.git.commit_sha is None
    assert recovered.git.checkpoint_attempts == 1
    assert recovered.git.checkpoint_base_sha == initial_sha
    assert recovered.git.rolled_back_checkpoint_attempt is None

    (tmp_path / "app.txt").write_text("second-pass\n", encoding="utf-8")
    second = run_next_task(tmp_path)

    assert second.result is not None
    assert second.result.final_status == "done"
    assert (
        _run(["git", "log", "-1", "--pretty=%s"], tmp_path)
        == "litehive: complete T-0001 recover-rerun (attempt 2)"
    )
    refreshed = require_task(tmp_path, "T-0001")
    assert refreshed.git.checkpoint_attempts == 2
    assert refreshed.git.checkpoint_base_sha == first.commit_sha
    assert refreshed.git.commit_sha == second.commit_sha


def test_recover_completed_task_rolls_back_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_recover_completed_task_rolls_back_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is None
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_recover_completed_task_rolls_back_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    queued = create_task(tmp_path, title="Keep queued")
    task = create_task(tmp_path, title="Recover without revert")
    task.status = "done"
    task.pipeline_status = "done"
    save_task(tmp_path, task)
    state = load_state(tmp_path)
    state.queue = [queued.id]
    save_state(tmp_path, state)

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        recover_completed_task(tmp_path, task.id)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is None
    assert load_state(tmp_path).queue == [queued.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Task recovered for another implementation pass." not in journal


def test_drain_task_pool_recovers_stranded_commit_stage_before_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit task")
    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{stranded.id}-{stranded.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("updated\n", encoding="utf-8")

    stranded.status = "done"
    stranded.pipeline_status = "done"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)
    stranded.git.commit_sha = None
    stranded.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, stranded)
    report_path = task_dir(tmp_path, stranded) / "reports" / "accepting-001.yaml"
    report_path.write_text(
        yaml.safe_dump(
            {
                "task_id": stranded.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "accepting complete",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )

    summary = drain_task_pool(tmp_path)

    assert [
        execution.task.id for execution in summary.executions if execution.task is not None
    ] == [stranded.id]
    assert summary.stop_reason == "queue_exhausted"
    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha is not None
    assert refreshed.runtime.execution_status == "done"
    assert refreshed.runtime.last_stage.step == "commit_to_git"
    assert refreshed.runtime.last_stage.verdict == "pass"
    assert load_state(tmp_path).queue == []


def test_commit_to_git_ignores_unrelated_main_checkout_changes_when_task_worktree_is_clean(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finalize isolated worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("updated from task worktree\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("unrelated main checkout dirt\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to commit",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha is not None


def test_commit_to_git_fast_forwards_main_when_worktree_commit_is_direct_descendant(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fast forward worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("fast-forwarded\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to integrate",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.git.commit_sha is not None
    assert task.git.checkpoint_base_sha == initial_sha
    assert _run(["git", "rev-parse", "HEAD^"], tmp_path) == initial_sha
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == task.git.commit_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "fast-forwarded\n"


def test_commit_to_git_cherry_picks_when_main_moved_after_worktree_started(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Cherry pick divergent worktree commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("from worktree\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready to integrate",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (tmp_path / "README.md").write_text("main moved\n", encoding="utf-8")
    _run(["git", "add", "README.md"], tmp_path)
    _run(["git", "commit", "-m", "main changed"], tmp_path)
    moved_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.git.commit_sha is not None
    assert task.git.checkpoint_base_sha == moved_sha
    assert _run(["git", "rev-parse", "HEAD^"], tmp_path) == moved_sha
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == task.git.commit_sha
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "from worktree\n"


def test_commit_to_git_treats_clean_task_worktree_as_done(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Already integrated task")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "already integrated",
                "files_changed": ["app.txt"],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)


def test_commit_to_git_integrates_existing_litehive_checkpoint_from_clean_worktree(
    tmp_path: Path,
) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover clean worktree checkpoint")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("checkpointed\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.git.checkpoint_attempts = 1
    task.git.checkpoint_base_sha = initial_sha
    save_task(tmp_path, task)
    commit_message = checkpoint_message(task, attempt=1)
    _run(["git", "add", "app.txt"], worktree_path)
    _run(["git", "commit", "-m", commit_message], worktree_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "checkpointed\n"


def test_commit_to_git_fails_when_agent_precommits_in_task_worktree(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent committed early")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "app.txt").write_text("agent-commit\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    _run(["git", "add", "app.txt"], worktree_path)
    _run(["git", "commit", "-m", "manual agent commit"], worktree_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "fail"
    assert "only Litehive may create the final task commit" in report.summary
    assert task.git.commit_sha is None
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == "initial"


def test_commit_to_git_treats_metadata_only_changes_as_done(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Metadata only commit")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / ".litehive" / "state.yaml").parent.mkdir(parents=True, exist_ok=True)
    (worktree_path / ".litehive" / "state.yaml").write_text("active_task_id: null\n", encoding="utf-8")

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)
    reports_dir = task_dir(tmp_path, task) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "metadata only",
                "files_changed": ["path/to/file", "none", "-", " N/A "],
                "tests": {"added": 0, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert task.git.commit_sha == _run(["git", "rev-parse", "HEAD"], tmp_path)


def test_resolve_next_task_finalizes_existing_checkpoint_commit_without_retry(tmp_path: Path) -> None:
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    stranded = create_task(tmp_path, title="Stranded commit task")
    new_task = create_task(tmp_path, title="Pending follow-up", auto_commit=False)
    (tmp_path / "app.txt").write_text("updated\n", encoding="utf-8")

    commit_message = "litehive: complete T-0001 stranded-commit-task"
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-m", commit_message], tmp_path)
    existing_checkpoint_sha = _run(["git", "rev-parse", "HEAD"], tmp_path)

    stranded.status = "done"
    stranded.pipeline_status = "done"
    stranded.git.checkpoint_attempts = 1
    stranded.git.checkpoint_base_sha = initial_sha
    stranded.git.commit_sha = None
    stranded.runtime.execution_status = "running"
    stranded.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, stranded)
    save_task_runtime(tmp_path, stranded)

    state = load_state(tmp_path)
    state.active_task_id = stranded.id
    state.queue = [new_task.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == new_task.id
    refreshed = get_task(tmp_path, stranded.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.commit_sha == existing_checkpoint_sha
    assert refreshed.git.checkpoint_attempts == 1
    assert refreshed.runtime.execution_status == "done"
    assert refreshed.runtime.last_stage.step == "commit_to_git"
    assert refreshed.runtime.last_stage.verdict == "pass"
    assert refreshed.runtime.current_stage.step is None
    assert load_state(tmp_path).queue == [new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered existing checkpoint commit after interrupted `commit_to_git`" in journal
    assert _run(["git", "log", "-1", "--pretty=%s"], tmp_path) == commit_message


def test_resolve_next_task_recovers_orphaned_commit_stage_before_new_work(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    new_task = create_task(tmp_path, title="New task", auto_commit=False)
    orphaned = create_task(tmp_path, title="Orphaned commit stage", auto_commit=False)

    orphaned.status = "in_progress"
    orphaned.pipeline_status = "commit_to_git"
    orphaned.runtime.execution_status = "running"
    orphaned.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, orphaned)
    save_task_runtime(tmp_path, orphaned)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [new_task.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == orphaned.id
    refreshed = get_task(tmp_path, orphaned.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert refreshed.runtime.run_started_at is None
    assert refreshed.runtime.current_stage.step == "commit_to_git"
    assert refreshed.runtime.current_stage.status == "interrupted"
    assert refreshed.runtime.last_outcome.kind == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal


def test_resolve_next_task_recovers_orphaned_interrupted_commit_stage_before_new_work(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    new_task = create_task(tmp_path, title="New task", auto_commit=False)
    orphaned = create_task(tmp_path, title="Halted commit stage", auto_commit=False)

    orphaned.status = "interrupted"
    orphaned.pipeline_status = "commit_to_git"
    orphaned.runtime.execution_status = "interrupted"
    orphaned.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="interrupted",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="blocked",
        summary="Interrupted `commit_to_git` run recovered. Resume from `commit_to_git`.",
    )
    save_task(tmp_path, orphaned)
    save_task_runtime(tmp_path, orphaned)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [new_task.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == orphaned.id
    refreshed = get_task(tmp_path, orphaned.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "interrupted"
    assert load_state(tmp_path).queue == [orphaned.id, new_task.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Interrupted runner execution while `commit_to_git` was running." in journal
    assert "Resume from `commit_to_git`." in journal


def test_resolve_next_task_recovers_flagged_commit_stage_after_passing_review(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    follow_up = create_task(tmp_path, title="Later task", auto_commit=False)
    flagged = create_task(tmp_path, title="Accepted but not committed", auto_commit=False)

    flagged.status = "flagged"
    flagged.pipeline_status = "commit_to_git"
    flagged.runtime.execution_status = "flagged"
    flagged.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="blocked",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="fail",
        summary="commit never ran",
    )
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)
    (task_dir(tmp_path, flagged) / "reports" / "testing-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "testing",
                "verdict": "pass",
                "summary": "ready for final commit",
                "files_changed": ["litehive/tasks.py"],
                "tests": {"added": 1, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == flagged.id
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id, follow_up.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_resolve_next_task_recovers_flagged_commit_stage_after_failed_commit_report(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    follow_up = create_task(tmp_path, title="Later task", auto_commit=False)
    flagged = create_task(tmp_path, title="Accepted but merge conflicted", auto_commit=False)

    flagged.status = "flagged"
    flagged.pipeline_status = "commit_to_git"
    flagged.runtime.execution_status = "flagged"
    flagged.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="blocked",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="fail",
        summary="CommitToGit failed: merge conflict while integrating task checkpoint",
    )
    flagged.runtime.last_outcome.kind = "flagged"
    flagged.runtime.last_outcome.stage = "commit_to_git"
    flagged.runtime.last_outcome.reason_code = "verdict_fail"
    flagged.runtime.last_outcome.reason = (
        "CommitToGit failed: merge conflict while integrating task checkpoint"
    )
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)
    reports_dir = task_dir(tmp_path, flagged) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready for final commit",
                "files_changed": ["litehive/tasks.py"],
                "tests": {"added": 1, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (reports_dir / "commit_to_git-002.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "commit_to_git",
                "verdict": "fail",
                "summary": "CommitToGit failed: merge conflict while integrating task checkpoint",
                "warnings": ["merge conflict while integrating task checkpoint"],
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [follow_up.id]
    save_state(tmp_path, state)

    task = resolve_next_task(tmp_path)

    assert task is not None
    assert task.id == flagged.id
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id, follow_up.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_repair_workspace_state_recovers_flagged_commit_stage_after_failed_commit_report(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    flagged = create_task(tmp_path, title="Repair conflicted integration", auto_commit=False)

    flagged.status = "flagged"
    flagged.pipeline_status = "commit_to_git"
    flagged.runtime.execution_status = "flagged"
    flagged.runtime.current_stage = RuntimeStageState(
        step="commit_to_git",
        status="blocked",
        started_at="2026-04-01T00:00:00+00:00",
        completed_at="2026-04-01T00:01:00+00:00",
        updated_at="2026-04-01T00:01:00+00:00",
        duration_seconds=60,
        verdict="fail",
        summary="CommitToGit failed: cherry-pick conflict while integrating task checkpoint",
    )
    save_task(tmp_path, flagged)
    save_task_runtime(tmp_path, flagged)
    reports_dir = task_dir(tmp_path, flagged) / "reports"
    (reports_dir / "accepting-001.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "accepting",
                "verdict": "pass",
                "summary": "ready for final commit",
                "files_changed": ["litehive/runtime.py"],
                "tests": {"added": 1, "passing": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (reports_dir / "commit_to_git-002.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": flagged.id,
                "step": "commit_to_git",
                "verdict": "fail",
                "summary": "CommitToGit failed: cherry-pick conflict while integrating task checkpoint",
                "warnings": ["cherry-pick conflict while integrating task checkpoint"],
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    state = load_state(tmp_path)
    state.queue = []
    save_state(tmp_path, state)

    summary = repair_workspace_state(tmp_path)

    assert summary.mutated is True
    assert summary.requeued_task_ids == [flagged.id]
    refreshed = get_task(tmp_path, flagged.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert load_state(tmp_path).queue == [flagged.id]
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Recovered flagged accepted task back to `queued/commit_to_git`" in journal


def test_rollback_completed_task_restores_state_when_rollback_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on commit failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)

    def fail_rollback_commit(root: Path, message: str):  # type: ignore[no-untyped-def]
        if message.startswith("litehive: rollback "):
            raise GitError("git rollback commit failed")
        return None

    monkeypatch.setattr("litehive.runtime.commit_task", fail_rollback_commit)

    with pytest.raises(GitError, match="git rollback commit failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert refreshed.git.rolled_back_checkpoint_attempt is None
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_atomic_state_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)

    original_atomic_write = tasks_module._atomic_write_text

    def fail_on_state_write(path: Path, content: str) -> None:
        if path == tmp_path / ".litehive" / "state.yaml":
            raise OSError("state write failed")
        original_atomic_write(path, content)

    monkeypatch.setattr("litehive.tasks._atomic_write_text", fail_on_state_write)

    with pytest.raises(OSError, match="state write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_task_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on task persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)
    task = require_task(tmp_path, "T-0001")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_file(tmp_path, task),
        message="task write failed",
    )

    with pytest.raises(OSError, match="task write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_rollback_completed_task_restores_state_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Rollback restore on persist failure")
    (tmp_path / "app.txt").write_text("broken\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)
    checkpoint_head = _run(["git", "rev-parse", "HEAD"], tmp_path)
    task = require_task(tmp_path, "T-0001")

    _fail_atomic_write_on_path(
        monkeypatch,
        task_runtime_file(tmp_path, task),
        message="runtime write failed",
    )

    with pytest.raises(OSError, match="runtime write failed"):
        rollback_completed_task(tmp_path, "T-0001")

    refreshed = get_task(tmp_path, "T-0001")
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.pipeline_status == "done"
    assert load_state(tmp_path).queue == []
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "broken\n"
    assert _run(["git", "rev-parse", "HEAD"], tmp_path) == checkpoint_head
    assert _git_status_without_litehive(tmp_path) == []
    journal = (task_dir(tmp_path, refreshed) / "journal.md").read_text(encoding="utf-8")
    assert "Checkpoint rollback requested." not in journal


def test_recover_command_reroutes_large_task_without_acceptance_criteria_to_grooming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Fix missing criteria",
        goal="Ship CLI tool",
        acceptance_criteria=["Task completes"],
    )
    task.priority = "high"
    save_task(tmp_path, task)
    (tmp_path / "app.txt").write_text("ship-again\n", encoding="utf-8")

    monkeypatch.setattr(
        "litehive.runtime.SubagentManager.run",
        lambda self, task, role, engine_name, prompt, model=None, max_turns=None: _completed_subagent_result(
            tmp_path, task.pipeline_status
        ),
    )
    run_next_task(tmp_path)
    update_task_metadata(tmp_path, task.id, acceptance_criteria=[])

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id=task.id))
    recover_output = capsys.readouterr().out

    assert exit_code == 0
    assert "pipeline_status: grooming" in recover_output
    assert "warning: Structured acceptance criteria are required before implementation for larger tasks." in recover_output
    assert "Use `--acceptance-criteria` to persist at least one structured bullet." not in recover_output
    recovered = get_task(tmp_path, task.id)
    assert recovered is not None
    assert recovered.pipeline_status == "grooming"
    assert load_state(tmp_path).queue == ["T-0001"]


def test_rollback_requires_completed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Not done yet")

    exit_code = _cmd_rollback(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not completed; cannot rollback" in output


def test_recover_requires_completed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Still queued")

    exit_code = _cmd_recover(argparse.Namespace(workspace=tmp_path, task_id="T-0001"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "is not completed; cannot recover" in output


def test_claude_build_invocation_includes_model_and_max_turns(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
        max_turns=15,
    )
    invocation = adapter.build_invocation(
        "ship it",
        tmp_path,
        model="claude-sonnet-4-20250514",
    )

    assert invocation.cwd == tmp_path
    assert list(invocation.argv) == [
        "claude",
        "-p",
        "ship it",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--model",
        "claude-sonnet-4-20250514",
        "--max-turns",
        "15",
    ]


def test_claude_default_max_turns_is_30(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )
    invocation = adapter.build_invocation("hello", tmp_path)

    assert "--max-turns" in invocation.argv
    idx = list(invocation.argv).index("--max-turns")
    assert list(invocation.argv)[idx + 1] == "30"


def test_claude_build_invocation_allows_max_turn_override(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
        max_turns=30,
    )
    invocation = adapter.build_invocation("hello", tmp_path, max_turns=7)

    assert "--max-turns" in invocation.argv
    idx = list(invocation.argv).index("--max-turns")
    assert list(invocation.argv)[idx + 1] == "7"


def test_run_next_task_passes_configured_claude_max_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True, claude_max_turns=7))
    create_task(tmp_path, title="Claude max turns task", engine="claude", auto_commit=False)
    calls: list[int | None] = []

    def fake_run(self, prompt, cwd, model=None, max_turns=None):  # type: ignore[no-untyped-def]
        calls.append(max_turns)
        return CLIExecutionResult(
            adapter="claude",
            argv=("claude", "-p"),
            cwd=cwd,
            exit_code=0,
            stdout="\n".join(
                [
                    "VERDICT: PASS",
                    "SUMMARY: ok",
                    "FILES_CHANGED:",
                    "TESTS_ADDED: 0",
                    "TESTS_PASSING: 0",
                    "WARNINGS:",
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("litehive.engines.ClaudeCLIAdapter.run", fake_run)

    summary = run_next_task(tmp_path)

    assert summary.task is not None
    assert summary.result is not None
    assert summary.result.final_status == "done"
    assert calls
    assert calls[0] == 7


def test_claude_renders_jsonl_transcript_and_stage_report(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"system","subtype":"init"}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"VERDICT: PASS\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"SUMMARY: implemented Claude adapter\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}]}}',
                '{"type":"assistant","message":{"content":[{"type":"text","text":"TESTS_ADDED: 3\\nTESTS_PASSING: 3\\nWARNINGS:\\n"}]}}',
                '{"type":"result","result":"done"}',
            ]
        ),
        stderr="",
    )

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )

    assert adapter.render_transcript(execution).splitlines()[0] == "VERDICT: PASS"
    report = adapter.parse_stage_report(
        task_id="T-0006",
        step="implementing",
        execution=execution,
        subagent_status="completed",
    )

    assert report.verdict == "pass"
    assert report.summary == "implemented Claude adapter"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 3, "passing": 3}


def test_claude_renders_partial_stream_events_for_live_capture(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"VERDICT: PASS\\n"}}',
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"SUMMARY: partial Claude output\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"TESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n"}}',
            ]
        ),
        stderr="",
    )

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )

    transcript = adapter.render_transcript(execution)
    assert transcript.splitlines()[0] == "VERDICT: PASS"
    report = adapter.parse_stage_report(
        task_id="T-0006",
        step="implementing",
        execution=execution,
        subagent_status="running",
    )

    assert report.summary == "partial Claude output"
    assert report.files_changed == ["litehive/engines.py"]
    assert report.tests == {"added": 1, "passing": 1}


def test_claude_live_progress_report_uses_adapter_summary_for_restart_snippet(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    task = create_task(tmp_path, title="Claude live restart summary", engine="claude", auto_commit=False)
    manager = SubagentManager(tmp_path)

    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="claude",
        status="running",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    mark_subagent_started(tmp_path, task, ref)
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=False)
    manager._write_session_start(base, ref, "stream partial Claude output")

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"VERDICT: PASS\\n"}}',
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"SUMMARY: partial Claude output\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"TESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n"}}',
            ]
        ),
        stderr="",
        pid=4242,
    )

    manager._write_session_progress(task, base, ref, "stream partial Claude output", execution)

    report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert report["status"] == "running"
    assert report["summary"] == "partial Claude output"
    assert report["files_changed"] == ["litehive/engines.py"]
    assert report["tests"] == {"added": 1, "passing": 1}

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    interrupted = tasks_module._mark_interrupted_subagent(
        tmp_path,
        refreshed,
        reason="runner interrupted before subagent completion",
        stage="implementing",
    )

    assert interrupted is not None
    assert interrupted.transcript_snippet == "partial Claude output"

    resumed_report = yaml.safe_load((base / "report.yaml").read_text(encoding="utf-8"))
    assert resumed_report["status"] == "interrupted"
    assert resumed_report["summary"] == "partial Claude output"
    assert resumed_report["resume_stage"] == "implementing"


def test_claude_stage_report_uses_error_when_no_assistant_message(tmp_path: Path) -> None:
    from litehive.engines import ClaudeCLIAdapter

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=1,
        stdout='{"type":"error","data":{"message":"authentication required"}}',
        stderr="",
    )

    adapter = ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    )
    report = adapter.parse_stage_report(
        task_id="T-0006",
        step="testing",
        execution=execution,
        subagent_status="failed",
    )

    assert report.summary == "authentication required"
    assert report.verdict == "blocked"


def test_resolve_engine_name_rejects_claude_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    config = load_config(tmp_path)
    assert config.claude_enabled is False

    task = create_task(tmp_path, title="Claude task", engine="claude")
    assert resolve_engine_name(task, config) == "claude"


def test_resolve_engine_name_rejects_default_claude_when_not_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    config = load_config(tmp_path)
    assert config.default_engine == "claude"
    assert config.claude_enabled is False

    task = create_task(tmp_path, title="Claude default task")
    assert resolve_engine_name(task, config) == "claude"


def test_resolve_engine_name_allows_claude_when_enabled(tmp_path: Path) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    config = load_config(tmp_path)
    assert config.claude_enabled is True

    task = create_task(tmp_path, title="Claude task", engine="claude")
    assert resolve_engine_name(task, config) == "claude"


def test_claude_is_not_default_engine() -> None:
    config = LitehiveConfig()
    assert config.default_engine != "claude"
    assert config.claude_enabled is False


def test_claude_config_defaults_to_sonnet() -> None:
    config = LitehiveConfig(claude_enabled=True)
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 30


def test_claude_not_in_engine_fallbacks() -> None:
    config = LitehiveConfig()
    for engine, fallbacks in config.engine_fallbacks.items():
        assert "claude" not in fallbacks, f"claude should not be a fallback for {engine}"


def test_claude_engine_in_registry() -> None:
    engine = get_engine("claude")
    assert engine.name == "claude"
    assert engine.capabilities.supports_model_override is True
    assert engine.capabilities.transcript_format == "jsonl"


def test_update_command_accepts_claude_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(claude_enabled=True))
    task = create_task(tmp_path, title="Tune Claude task")

    exit_code = _cmd_update(
        argparse.Namespace(
            workspace=tmp_path,
            task_id=task.id,
            engine="claude",
            acceptance_criteria=None,
            priority=None,
            goal=None,
            mode=None,
            auto_commit=None,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    updated = get_task(tmp_path, task.id)
    assert updated is not None
    assert updated.engine == "claude"
    assert "engine: claude" in output


def test_configure_persists_claude_settings(tmp_path: Path) -> None:
    from litehive.cli import _cmd_configure

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="generic",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_enabled=True,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=20,
        pool_usage_cap=12,
        pool_cost_cap=30,
        engine_usage_cap=["claude=2", "codex=5"],
        engine_budget_cap=["claude=6"],
        engine_cost=["claude=3", "codex=1"],
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
    )

    assert _cmd_configure(parser) == 0
    config = load_config(tmp_path)
    assert config.claude_enabled is True
    assert config.claude_model == "claude-sonnet-4-20250514"
    assert config.claude_max_turns == 20
    assert config.pool_usage_cap == 12
    assert config.pool_cost_cap == 30
    assert config.engine_usage_caps == {"claude": 2, "codex": 5}
    assert config.engine_budget_caps == {"claude": 6}
    assert config.engine_costs["claude"] == 3
    assert config.task_engine_routing["research"][0] == "gemini"


def test_configure_updates_existing_workspace_budget_settings(tmp_path: Path) -> None:
    from litehive.cli import _cmd_configure

    ensure_workspace(
        tmp_path,
        LitehiveConfig(
            process_profile="generic",
            pool_usage_cap=4,
            pool_cost_cap=8,
            engine_usage_caps={"codex": 2},
            engine_budget_caps={"claude": 3},
            engine_costs={"codex": 1, "claude": 3},
        ),
    )

    parser = argparse.Namespace(
        workspace=tmp_path,
        default_engine="codex",
        process_profile="python",
        default_retry_limit=3,
        opencode_model="zai-coding-plan/glm-5.1",
        gemini_model=None,
        copilot_model=None,
        claude_enabled=True,
        claude_model="claude-sonnet-4-20250514",
        claude_max_turns=20,
        pool_usage_cap=12,
        pool_cost_cap=30,
        engine_usage_cap=["claude=2", "codex=5"],
        engine_budget_cap=["claude=6"],
        engine_cost=["claude=3", "codex=1"],
        task_engine_route=None,
        pool_stop_on_failure=False,
        pool_max_tasks=None,
        pool_stop_on_limit=False,
        pool_quota_threshold=None,
        pool_budget_threshold=None,
        pool_stop_on_dirty_git=False,
        pool_selection_policy="dependency_aware",
        pre_acceptance_command=None,
    )

    assert _cmd_configure(parser) == 0

    config = load_config(tmp_path)
    assert config.process_profile == "python"
    assert config.claude_enabled is True
    assert config.claude_max_turns == 20
    assert config.pool_usage_cap == 12
    assert config.pool_cost_cap == 30
    assert config.engine_usage_caps == {"claude": 2, "codex": 5}
    assert config.engine_budget_caps == {"claude": 6}
    assert config.engine_costs["claude"] == 3
    assert config.engine_costs["codex"] == 1
    assert config.engine_costs["opencode"] == 1
    assert config.engine_costs["gemini"] == 1
    assert config.engine_costs["copilot"] == 1
    context = (tmp_path / ".litehive" / "context.md").read_text(encoding="utf-8")
    assert "Process profile: Python" in context


def test_claude_model_resolved_from_workspace_defaults() -> None:
    from litehive.runtime import workspace_model_for_engine

    config = LitehiveConfig(claude_model="claude-sonnet-4-20250514")
    assert workspace_model_for_engine(config, "claude") == "claude-sonnet-4-20250514"

    config_default = LitehiveConfig()
    assert workspace_model_for_engine(config_default, "claude") == "claude-sonnet-4-20250514"


def test_cmd_run_dry_run_rejects_default_claude_when_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    create_task(tmp_path, title="Claude default task")

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_single_task should not be called for dry-run")

    def fail_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain_task_pool should not be called for dry-run")

    monkeypatch.setattr("litehive.cli.run_single_task", fail_run_single)
    monkeypatch.setattr("litehive.cli.drain_task_pool", fail_drain)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=True, engine=None))
    assert exit_code == 0


def test_cmd_run_dispatches_single_task_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    called: list[str] = []

    def fake_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        called.append("single")
        return 0

    def fail_run_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("drain handler should not run for single-task mode")

    monkeypatch.setattr("litehive.cli._cmd_run_single", fake_run_single)
    monkeypatch.setattr("litehive.cli._cmd_run_drain", fail_run_drain)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=False))

    assert exit_code == 0
    assert called == ["single"]


def test_cmd_run_dispatches_pool_drain_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    called: list[str] = []

    def fail_run_single(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("single-task handler should not run for drain mode")

    def fake_run_drain(*args, **kwargs):  # type: ignore[no-untyped-def]
        called.append("drain")
        return 0

    monkeypatch.setattr("litehive.cli._cmd_run_single", fail_run_single)
    monkeypatch.setattr("litehive.cli._cmd_run_drain", fake_run_drain)

    exit_code = _cmd_run(argparse.Namespace(workspace=tmp_path, dry_run=False, drain=True))

    assert exit_code == 0
    assert called == ["drain"]
