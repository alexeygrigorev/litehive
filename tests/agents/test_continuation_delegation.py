from pathlib import Path
from types import SimpleNamespace

from heru import get_engine
from heru.base import CLIExecutionResult
from heru.types import RuntimeEngineContinuation
from litehive.agents.session import SessionMixin
from litehive.config.engine_models import _set_continuation_handoff
from litehive.heru_compat import extract_engine_continuation
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


def test_session_mixin_extract_execution_continuation_delegates_to_heru_for_supported_engines(monkeypatch) -> None:
    captured: list[str] = []

    def fake_extract(engine_name, execution):  # type: ignore[no-untyped-def]
        captured.append(engine_name)
        assert execution is not None
        return RuntimeEngineContinuation(session_id=f"{engine_name}-session")

    monkeypatch.setattr("litehive.agents.session.extract_engine_continuation", fake_extract)

    for engine_name in ("codex", "claude", "copilot", "gemini", "goz", "opencode"):
        continuation = SessionMixin._extract_execution_continuation(engine_name, _execution("plain stdout"))
        assert continuation is not None
        assert continuation.resume_id == f"{engine_name}-session"

    assert captured == ["codex", "claude", "copilot", "gemini", "goz", "opencode"]


def test_set_continuation_handoff_preserves_unified_continuation_payload(tmp_path, monkeypatch) -> None:
    task = create_task(tmp_path, title="Continuation handoff", auto_commit=False)

    class FakeEngine:
        def render_transcript(self, execution):  # type: ignore[no-untyped-def]
            return "rendered transcript"

        def parse_stage_report(self, **kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(summary="summary", warnings=["warning"])

    monkeypatch.setattr("litehive.config.engine_models.get_engine", lambda _: FakeEngine())

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
        ref=SimpleNamespace(
            id="SA-0001-swe",
            path="subagents/SA-0001-swe",
            status="interrupted",
        ),
    )

    handoff = _set_continuation_handoff(
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
