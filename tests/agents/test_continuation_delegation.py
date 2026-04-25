from pathlib import Path
from types import SimpleNamespace

import pytest

from heru import extract_engine_continuation, get_engine
from heru.base import CLIExecutionResult
from heru.types import LiveTimeline, RuntimeEngineContinuation
from litehive.agents.session import SessionMixin
from litehive.config.engine_models import set_continuation_handoff
from litehive.state.records import create_task


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
def test_session_mixin_does_not_parse_native_engine_jsonl_fallback_for_supported_engines(engine_name: str) -> None:
    execution = _execution('{"type":"message","role":"assistant","content":"legacy output"}\n')

    transcript = SessionMixin.render_execution_transcript(engine_name, execution)
    continuation = SessionMixin.extract_execution_continuation(engine_name, execution)
    timeline = SessionMixin.extract_execution_timeline(engine_name, execution.stdout)

    assert transcript == execution.transcript
    assert continuation is None
    assert timeline is None


@pytest.mark.parametrize("engine_name", ("codex", "claude", "copilot", "gemini", "goz", "opencode"))
def test_session_mixin_extract_execution_continuation_delegates_to_heru(
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

    continuation = SessionMixin.extract_execution_continuation(engine_name, execution)

    assert continuation is expected


@pytest.mark.parametrize("engine_name", ("codex", "claude", "copilot", "gemini", "goz", "opencode"))
def test_session_mixin_consumes_unified_event_types_for_supported_engines(engine_name: str) -> None:
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

    transcript = SessionMixin.render_execution_transcript(engine_name, execution)
    continuation = SessionMixin.extract_execution_continuation(engine_name, execution)
    timeline = SessionMixin.extract_execution_timeline(
        engine_name,
        execution.stdout,
        task_id="T-0001",
        subagent_id="SA-0001",
    )

    assert transcript == "implemented via unified events"
    assert continuation is not None
    assert continuation.resume_id == "session-42"
    assert isinstance(timeline, LiveTimeline)
    assert timeline.engine == engine_name
    assert timeline.task_id == "T-0001"
    assert timeline.subagent_id == "SA-0001"
    assert [event.kind for event in timeline.events] == ["message", "continuation"]


def test_set_continuation_handoff_preserves_unified_continuation_payload(tmp_path, monkeypatch) -> None:
    task = create_task(tmp_path, title="Continuation handoff", auto_commit=False)

    captured = {}

    def capture_handoff(root, task_record, handoff):  # type: ignore[no-untyped-def]
        captured["root"] = root
        captured["task_id"] = task_record.id
        captured["handoff"] = handoff

    monkeypatch.setattr(
        "litehive.config.engine_models.set_task_continuation_handoff",
        capture_handoff,
    )

    execution = _execution(
        '{"kind":"message","engine":"codex","sequence":0,'
        '"role":"assistant","content":"implemented via unified events",'
        '"timestamp":"2026-04-12T00:00:00+00:00","usage_delta":{},'
        '"raw":{},"metadata":{}}\n'
        '{"kind":"continuation","engine":"codex","sequence":1,'
        '"timestamp":"2026-04-12T00:00:01+00:00","continuation_id":"session-42",'
        '"usage_delta":{},"raw":{},"metadata":{}}\n'
    )
    result = SimpleNamespace(
        transcript="implemented via unified events",
        execution=execution,
        continuation=RuntimeEngineContinuation(session_id="session-42"),
        ref=SimpleNamespace(
            id="SA-0001-swe",
            path="subagents/SA-0001-swe",
            status="interrupted",
        ),
    )

    handoff = set_continuation_handoff(
        tmp_path,
        task,
        stage="implementing",
        kind="restart",
        reason="retry",
        result=result,
        from_engine="codex",
        to_engine="codex",
        from_model=None,
        to_model=None,
        attempt=2,
    )

    assert handoff.continuation is not None
    assert handoff.continuation.resume_id == "session-42"
    assert captured["task_id"] == task.id
    assert captured["handoff"].continuation.resume_id == "session-42"
    assert handoff.transcript_snippet == "implemented via unified events"
