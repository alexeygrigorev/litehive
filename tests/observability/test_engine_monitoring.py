from pathlib import Path

from heru import get_engine
from heru.base import AdapterCapabilities, CLIExecutionResult, ExternalCLIAdapter
from litehive.config.workspace import ensure_workspace
from litehive.domain.engine import EngineUsageObservation, EngineUsageWindow
from litehive.observability.engine_monitoring import (
    load_engine_monitoring,
    record_engine_execution,
)
from litehive.workspace import Workspace


def test_record_engine_execution_tracks_local_usage_fallback(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    record_engine_execution(
        Workspace.from_path(tmp_path),
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

    monitoring = load_engine_monitoring(Workspace.from_path(tmp_path))
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
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            resume_session_id: str | None = None,
        ) -> list[str]:
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
        Workspace.from_path(tmp_path),
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

    monitoring = load_engine_monitoring(Workspace.from_path(tmp_path))
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
        Workspace.from_path(tmp_path),
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

    monitoring = load_engine_monitoring(Workspace.from_path(tmp_path))
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
        Workspace.from_path(tmp_path),
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

    monitoring = load_engine_monitoring(Workspace.from_path(tmp_path))
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
    assert record.metadata["error_message"] == ("Your account has hit a rate limit. Please retry after a short delay.")


def test_record_engine_execution_tracks_opencode_provider_usage_observation(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    record_engine_execution(
        Workspace.from_path(tmp_path),
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

    monitoring = load_engine_monitoring(Workspace.from_path(tmp_path))
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


def test_load_engine_monitoring_ignores_legacy_workspace_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    monitoring_file = tmp_path / ".litehive" / "engine-monitoring.yaml"
    monitoring_file.write_text(
        "engines:\n  codex:\n    engine: codex\n    last_limit_kind: capacity\n",
        encoding="utf-8",
    )

    monitoring = load_engine_monitoring(Workspace.from_path(tmp_path))

    assert monitoring.engines == {}
