import logging
from pathlib import Path

from heru import (
    extract_engine_continuation,
    extract_engine_timeline,
    get_engine,
    parse_unified_execution,
    render_execution_transcript,
)
from heru.base import CLIExecutionResult


def test_parse_unified_execution_logs_invalid_payload(caplog) -> None:
    stdout = '{"kind":"message","content":123}\n'

    with caplog.at_level(logging.WARNING, logger="heru.unified_events"):
        parsed = parse_unified_execution(stdout)

    assert parsed is None
    assert "Discarding invalid unified event payload" in caplog.text


def test_codex_multiline_command_execution_uses_fallback_without_jsonl_warning(caplog) -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread_123"}',
            "{",
            '  "type": "item.completed",',
            '  "item": {',
            '    "id": "item_1",',
            '    "type": "command_execution",',
            '    "command": [',
            '      "bash",',
            '      "-lc",',
            '      "uv run pytest -q"',
            "    ],",
            '    "aggregated_output": "tests failed",',
            '    "exit_code": 1,',
            '    "status": "failed"',
            "  }",
            "}",
            "{",
            '  "type": "turn.completed",',
            '  "usage": {',
            '    "input_tokens": 10,',
            '    "output_tokens": 5,',
            '    "total_tokens": 15',
            "  }",
            "}",
        ]
    )
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=Path.cwd(),
        exit_code=1,
        stdout=stdout,
        stderr="",
    )

    with caplog.at_level(logging.WARNING):
        transcript = render_execution_transcript(
            execution,
            fallback_renderer=get_engine("codex").render_transcript,
        )
        timeline = extract_engine_timeline("codex", stdout)
        continuation = extract_engine_continuation("codex", execution)

    assert transcript == "tests failed"
    assert timeline is not None
    assert [event.kind for event in timeline.events] == ["tool_result", "usage"]
    assert continuation is not None
    assert continuation.resume_id == "thread_123"
    assert not any("iter_jsonl_payloads" in record.message for record in caplog.records)
