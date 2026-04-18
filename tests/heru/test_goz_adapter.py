from pathlib import Path

import pytest

from heru.adapters._goz_impl import goz_continuation
from heru.adapters.goz import GozCLIAdapter
from heru.base import CLIExecutionResult


def test_goz_adapter_extracts_continuation_from_observed_jsonl_output(tmp_path: Path) -> None:
    adapter = GozCLIAdapter()
    execution = CLIExecutionResult(
        adapter="goz",
        argv=("goz", "run", "--format", "json"),
        cwd=tmp_path,
        exit_code=0,
        stdout=(
            '{"type":"text","part":{"text":"hi"},"timestamp":"2026-04-18T20:35:53.984Z"}\n'
            '{"type":"step_finish","part":{"tokens":{"input":0,"output":2,"cache_read":0,"cache_creation":0},'
            '"cost":0.000016,"session_id":"d5ddc037b79c4d9fb10ccc9eb165f0c8",'
            '"continuation":{"resume_session_id":"d5ddc037b79c4d9fb10ccc9eb165f0c8"}},'
            '"timestamp":"2026-04-18T20:35:54.012Z"}\n'
        ),
        stderr="",
    )

    continuation = adapter.extract_continuation(execution)

    assert continuation is not None
    assert continuation.session_id == "d5ddc037b79c4d9fb10ccc9eb165f0c8"
    assert continuation.resume_id == "d5ddc037b79c4d9fb10ccc9eb165f0c8"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "type": "step_finish",
                "part": {"session_id": "resume-snake"},
            },
            "resume-snake",
        ),
        (
            {
                "type": "step_finish",
                "part": {"continuation": {"resumeSessionId": "resume-camel"}},
            },
            "resume-camel",
        ),
        (
            {
                "type": "step_finish",
                "continuation": {"sessionId": "resume-root"},
            },
            "resume-root",
        ),
    ],
)
def test_goz_continuation_extracts_supported_session_id_fields(
    payload: dict[str, object],
    expected: str,
) -> None:
    continuation = goz_continuation(payload)

    assert continuation is not None
    assert continuation.session_id == expected


def test_goz_adapter_build_command_includes_resume_session_id(tmp_path: Path) -> None:
    adapter = GozCLIAdapter()

    command = adapter.build_command(
        "Reply with hi",
        tmp_path,
        model="glm-5-turbo",
        resume_session_id="resume-123",
    )

    assert command == [
        "goz",
        "run",
        "--format",
        "json",
        "--resume-session",
        "resume-123",
        "--model",
        "glm-5-turbo",
        "Reply with hi",
    ]
