import logging
from pathlib import Path

from heru import (
    extract_engine_continuation,
    extract_engine_timeline as extract_engine_event_stream,
    get_engine,
    render_execution_transcript as render_execution_trace,
)
from heru.base import CLIExecutionResult

from litehive.agents.execution_trace import ParsedUnifiedEvents, execution_trace_renderer


def test_parse_unified_events_returns_named_event_collection() -> None:
    parsed = execution_trace_renderer().parse_unified_events('{"kind":"message","engine":"codex","content":"step"}')

    assert isinstance(parsed, ParsedUnifiedEvents)
    assert len(parsed.events) == 1
    assert parsed.events[0].content == "step"


def test_codex_multiline_command_execution_extracts_trace_event_stream_and_continuation(caplog) -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread_123"}',
            (
                '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":['
                '"bash","-lc","uv run pytest -q"],"aggregated_output":"tests failed","exit_code":1,"status":"failed"}}'
            ),
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}',
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
        execution_trace = render_execution_trace(
            "codex",
            execution,
            fallback_renderer=get_engine("codex").render_transcript,
        )
        event_stream = extract_engine_event_stream("codex", stdout)
        continuation = extract_engine_continuation("codex", execution)

    assert execution_trace == "tests failed"
    assert event_stream is not None
    assert [event.kind for event in event_stream.events] == ["tool_result", "usage"]
    assert continuation is not None
    assert continuation.resume_id == "thread_123"
    assert not caplog.records
    assert not any("iter_jsonl_payloads" in record.message for record in caplog.records)


def test_unified_output_logs_parse_failures_with_line_context(caplog) -> None:
    stdout = "\n".join(
        [
            "not-json",
            '{"kind":"message","engine":"codex","content":"step 1"}',
            '{"kind":"message","content":"missing engine"}',
            '{"kind":"continuation","engine":"codex","continuation_id":"thread_456"}',
            '{"kind":"status","engine":"codex","content":"step 2"}',
        ]
    )
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=Path.cwd(),
        exit_code=0,
        stdout=stdout,
        stderr="",
    )

    with caplog.at_level(logging.WARNING):
        execution_trace = render_execution_trace(
            "codex",
            execution,
            fallback_renderer=get_engine("codex").render_transcript,
        )
        event_stream = extract_engine_event_stream("codex", stdout)
        continuation = extract_engine_continuation("codex", execution)

    assert execution_trace == "step 1\n\nstep 2"
    assert event_stream is not None
    assert [event.kind for event in event_stream.events] == ["message", "continuation", "status"]
    assert continuation is not None
    assert continuation.resume_id == "thread_456"

    messages = [record.message for record in caplog.records]
    assert any("unparseable JSONL line 1" in message and "not-json" in message for message in messages)
    assert any(
        "invalid unified event at line 3" in message and "engine: Field required" in message for message in messages
    )


def test_invalid_unified_output_logs_warning_before_fallback_transcript(caplog) -> None:
    stdout = '{"kind":"message","content":"missing engine"}'
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=Path.cwd(),
        exit_code=0,
        stdout=stdout,
        stderr="",
    )

    def fallback_renderer(_: CLIExecutionResult) -> str:
        return "fallback transcript"

    with caplog.at_level(logging.WARNING):
        execution_trace = render_execution_trace("codex", execution, fallback_renderer=fallback_renderer)

    assert execution_trace == "fallback transcript"
    messages = [record.message for record in caplog.records]
    assert any(
        "invalid unified event at line 1" in message and "engine: Field required" in message for message in messages
    )
    assert any("found no valid events" in message for message in messages)


def test_plain_text_output_falls_back_without_unified_parse_warnings(caplog) -> None:
    stdout = "tests failed\nsee details below"
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=Path.cwd(),
        exit_code=0,
        stdout=stdout,
        stderr="",
    )

    def fallback_renderer(execution: CLIExecutionResult) -> str:
        return execution.transcript

    with caplog.at_level(logging.WARNING):
        execution_trace = render_execution_trace("codex", execution, fallback_renderer=fallback_renderer)
        event_stream = extract_engine_event_stream("codex", stdout)
        continuation = extract_engine_continuation("codex", execution)

    assert execution_trace == stdout
    assert event_stream is None
    assert continuation is None


def test_malformed_jsonl_without_unified_candidates_logs_warnings_before_fallback(caplog) -> None:
    def fallback_renderer(_: CLIExecutionResult) -> str:
        return "fallback transcript"

    cases = [
        (
            '{"foo":"bar"}\n42',
            (
                "without unified event kind at line 1",
                "non-object JSONL line 2",
            ),
        ),
        (
            '{"foo":"bar"}\nnot-json',
            (
                "without unified event kind at line 1",
                "unparseable JSONL line 2",
            ),
        ),
    ]

    for stdout, expected_fragments in cases:
        execution = CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=Path.cwd(),
            exit_code=0,
            stdout=stdout,
            stderr="",
        )

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            execution_trace = render_execution_trace("codex", execution, fallback_renderer=fallback_renderer)
            event_stream = extract_engine_event_stream("codex", stdout)
            continuation = extract_engine_continuation("codex", execution)

        assert execution_trace == "fallback transcript"
        assert event_stream is None
        assert continuation is None

        messages = [record.message for record in caplog.records]
        for fragment in expected_fragments:
            assert any(fragment in message for message in messages)
