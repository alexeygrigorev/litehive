from pathlib import Path

import pytest

from heru import extract_engine_continuation, get_engine
from heru.base import CLIExecutionResult
from litehive.agents.execution_trace import execution_trace_renderer
from litehive.agents.session import SubagentSessionManager
from litehive.domain.engine import LiveEventStream
from heru.types import RuntimeEngineContinuation


def _execution(stdout: str) -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=Path.cwd(),
        exit_code=0,
        stdout=stdout,
        stderr="",
    )


def test_heru_extract_engine_continuation_prefers_unified_events(monkeypatch) -> None:
    adapter = get_engine("codex")

    def fail_if_called(execution):  # type: ignore[no-untyped-def]
        raise AssertionError("Adapter fallback should not run for codex")

    monkeypatch.setattr(adapter, "extract_continuation", fail_if_called)

    execution = _execution(
        '{"kind":"message","engine":"codex","sequence":0,'
        '"role":"assistant","content":"done","timestamp":"2026-04-12T00:00:00+00:00",'
        '"usage_delta":{},"raw":{},"metadata":{}}\n'
        '{"kind":"continuation","engine":"codex","sequence":1,'
        '"timestamp":"2026-04-12T00:00:01+00:00","continuation_id":"session-42",'
        '"usage_delta":{},"raw":{},"metadata":{}}\n'
    )

    continuation = extract_engine_continuation("codex", execution)

    assert continuation is not None
    assert continuation.resume_id == "session-42"


@pytest.mark.parametrize("engine_name", ("codex", "claude", "copilot", "gemini", "goz", "opencode"))
def test_subagent_session_manager_does_not_parse_native_engine_jsonl_fallback_for_supported_engines(engine_name: str) -> None:
    execution = _execution('{"type":"message","role":"assistant","content":"legacy output"}\n')

    execution_trace = execution_trace_renderer().render(execution)
    continuation = SubagentSessionManager.extract_execution_continuation(engine_name, execution)
    event_stream = SubagentSessionManager.extract_execution_event_stream(engine_name, execution.stdout)

    assert execution_trace == execution.transcript
    assert continuation is None
    assert event_stream is None


@pytest.mark.parametrize("engine_name", ("codex", "claude", "copilot", "gemini", "goz", "opencode"))
def test_subagent_session_manager_extract_execution_continuation_delegates_to_heru(
    engine_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution('{"type":"message","role":"assistant","content":"legacy output"}\n')
    expected = RuntimeEngineContinuation(session_id=f"{engine_name}-session")

    def fake_extract(requested_engine_name, requested_execution):  # type: ignore[no-untyped-def]
        assert requested_engine_name == engine_name
        assert requested_execution is execution
        return expected

    monkeypatch.setattr("litehive.agents.session.extract_engine_continuation", fake_extract)

    continuation = SubagentSessionManager.extract_execution_continuation(engine_name, execution)

    assert continuation is expected


@pytest.mark.parametrize("engine_name", ("codex", "claude", "copilot", "gemini", "goz", "opencode"))
def test_subagent_session_manager_consumes_unified_event_types_for_supported_engines(engine_name: str) -> None:
    execution = _execution(
        "\n".join(
            [
                (
                    f'{{"kind":"message","engine":"{engine_name}","sequence":0,'
                    '"role":"assistant","content":"implemented via unified events",'
                    '"timestamp":"2026-04-12T00:00:00+00:00","usage_delta":{},'
                    '"raw":{},"metadata":{}}'
                ),
                (
                    f'{{"kind":"continuation","engine":"{engine_name}","sequence":1,'
                    '"timestamp":"2026-04-12T00:00:01+00:00","continuation_id":"session-42",'
                    '"usage_delta":{},"raw":{},"metadata":{}}'
                ),
            ]
        )
    )

    execution_trace = execution_trace_renderer().render(execution)
    continuation = SubagentSessionManager.extract_execution_continuation(engine_name, execution)
    event_stream = SubagentSessionManager.extract_execution_event_stream(
        engine_name,
        execution.stdout,
        task_id="T-0001",
        subagent_id="SA-0001",
    )

    assert execution_trace == "implemented via unified events"
    assert continuation is not None
    assert continuation.resume_id == "session-42"
    assert isinstance(event_stream, LiveEventStream)
    assert event_stream.engine == engine_name
    assert event_stream.task_id == "T-0001"
    assert event_stream.subagent_id == "SA-0001"
    assert [event.kind for event in event_stream.events] == ["message", "continuation"]
