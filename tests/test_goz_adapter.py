from heru.adapters._goz_impl import goz_continuation, goz_session_id
from tests.workspace_helpers import CLIExecutionResult, Path, RuntimeEngineContinuation, get_engine


def test_goz_adapter_reports_model_override_support() -> None:
    assert get_engine("goz").capabilities.supports_model_override is True


def test_goz_build_invocation_includes_resume_session(tmp_path: Path) -> None:
    invocation = get_engine("goz").build_invocation(
        "continue please",
        tmp_path,
        resume_session_id="ses-123",
    )

    assert list(invocation.argv) == [
        "goz",
        "run",
        "--format",
        "json",
        "--resume-session",
        "ses-123",
        "continue please",
    ]


def test_goz_build_invocation_includes_model(tmp_path: Path) -> None:
    invocation = get_engine("goz").build_invocation(
        "continue please",
        tmp_path,
        model="glm-4.5",
    )

    assert list(invocation.argv) == [
        "goz",
        "run",
        "--format",
        "json",
        "--model",
        "glm-4.5",
        "continue please",
    ]


def test_goz_session_helpers_extract_step_finish_session_id() -> None:
    payload = {
        "type": "step_finish",
        "sessionID": "ses_123",
        "part": {"type": "step-finish", "reason": "stop"},
    }

    assert goz_session_id(payload) == "ses_123"
    assert goz_continuation(payload) == RuntimeEngineContinuation(session_id="ses_123")


def test_goz_continuation_ignores_non_step_finish_payloads() -> None:
    payload = {
        "type": "text",
        "sessionID": "ses_123",
        "part": {"type": "text", "text": "OK"},
    }

    assert goz_continuation(payload) is None


def test_goz_adapter_extracts_continuation_from_step_finish_payload(tmp_path: Path) -> None:
    execution = CLIExecutionResult(
        adapter="goz",
        argv=("goz", "run", "--format", "json"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"step_start","timestamp":1,"sessionID":"ses_123","part":{"id":"prt_1","type":"step-start"}}',
                '{"type":"text","timestamp":2,"sessionID":"ses_123","part":{"id":"prt_2","type":"text","text":"OK"}}',
                '{"type":"step_finish","timestamp":3,"sessionID":"ses_123","part":{"id":"prt_3","type":"step-finish","reason":"stop"}}',
            ]
        ),
        stderr="",
    )

    assert get_engine("goz").extract_continuation(execution) == RuntimeEngineContinuation(
        session_id="ses_123"
    )
