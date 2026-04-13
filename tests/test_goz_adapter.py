from pathlib import Path

import pytest

from heru.adapters._goz_impl import goz_continuation, goz_session_id
from heru.adapters.goz import GozCLIAdapter
from heru.base import CLIExecutionResult, LATEST_CONTINUATION_SENTINEL


def _execution(stdout: str) -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter="goz",
        argv=("goz", "run", "--format", "json"),
        cwd=Path.cwd(),
        exit_code=0,
        stdout=stdout,
        stderr="",
    )


def test_goz_session_id_extracts_non_empty_session_id() -> None:
    assert goz_session_id({"sessionID": "session-123"}) == "session-123"
    assert goz_session_id({"sessionID": ""}) is None
    assert goz_session_id({"sessionID": 123}) is None


def test_goz_continuation_only_returns_step_finish_session() -> None:
    assert goz_continuation({"type": "message", "sessionID": "session-123"}) is None

    continuation = goz_continuation({"type": "step_finish", "sessionID": "session-123"})

    assert continuation is not None
    assert continuation.resume_id == "session-123"


def test_goz_build_command_appends_resume_session_id() -> None:
    adapter = GozCLIAdapter()

    command = adapter.build_command(
        "continue",
        Path.cwd(),
        resume_session_id="session-123",
    )

    assert command == [
        "goz",
        "run",
        "--format",
        "json",
        "--resume-session",
        "session-123",
        "continue",
    ]


def test_goz_build_command_rejects_latest_resume_sentinel() -> None:
    adapter = GozCLIAdapter()

    with pytest.raises(ValueError, match="goz does not support resuming the latest session"):
        adapter.build_command(
            "continue",
            Path.cwd(),
            resume_session_id=LATEST_CONTINUATION_SENTINEL,
        )


def test_goz_extract_continuation_returns_step_finish_session() -> None:
    adapter = GozCLIAdapter()
    execution = _execution(
        '{"type":"message","sessionID":"ignored"}\n'
        '{"type":"step_finish","sessionID":"session-123"}\n'
        '{"type":"step_finish","sessionID":"session-456"}\n'
    )

    continuation = adapter.extract_continuation(execution)

    assert continuation is not None
    assert continuation.resume_id == "session-123"
