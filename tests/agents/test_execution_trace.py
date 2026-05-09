from pathlib import Path

from heru.types import SubagentRef
from heru.types import UnifiedEvent

from litehive.agents.execution_trace import (
    ParsedUnifiedEvents,
    execution_trace_renderer,
)
from litehive.agents.session_store import SubagentEventStreamPayload, subagent_artifacts
from litehive.config.workspace import create_workspace
from litehive.domain.common import utcnow
from litehive.domain.runtime import RuntimeSubagentState
from litehive.state.records import WorkspaceTasks
from litehive.workspace import Workspace


def test_parse_unified_events_ignores_noise_and_keeps_valid_events() -> None:
    parsed = execution_trace_renderer().parse_unified_events(
        "\n".join(
            [
                "plain output",
                '{"kind":"message","engine":"codex","content":"step 1"}',
                '{"kind":"message","content":"missing engine"}',
                '{"kind":"status","engine":"codex","content":"step 2"}',
            ]
        )
    )

    assert [event.kind for event in parsed.events] == ["message", "status"]
    assert [event.content for event in parsed.events] == ["step 1", "step 2"]


def test_render_event_for_execution_trace_formats_tool_blocks() -> None:
    event = UnifiedEvent.model_validate(
        {
            "kind": "tool_result",
            "engine": "codex",
            "tool_name": "pytest",
            "tool_input": "uv run pytest -q\n",
            "tool_output": "1 passed\n",
        }
    )

    assert execution_trace_renderer().render_event(event) == "\n".join(
        [
            "```tool",
            "name: pytest",
            "input:",
            "uv run pytest -q",
            "output:",
            "1 passed",
            "```",
        ]
    )


def test_render_execution_trace_from_events_appends_stderr() -> None:
    events = ParsedUnifiedEvents(
        events=(
            UnifiedEvent.model_validate({"kind": "message", "engine": "codex", "content": "implemented"}),
        )
    )

    trace = execution_trace_renderer().render_from_events(events, stderr="warning\n")

    assert trace == "implemented\n\n[stderr]\nwarning"


def test_render_execution_trace_from_streams_uses_unified_events_or_plain_text_fallback() -> None:
    unified_trace = execution_trace_renderer().render_from_streams(
        stdout='{"kind":"message","engine":"codex","content":"structured"}',
        stderr="",
    )
    plain_trace = execution_trace_renderer().render_from_streams(stdout="plain transcript\n", stderr="warn\n")

    assert unified_trace == "structured"
    assert plain_trace == "plain transcript\n\n[stderr]\nwarn"


def test_render_execution_trace_from_event_stream_payload_skips_invalid_events() -> None:
    trace = execution_trace_renderer().render_from_payload(
        {
            "events": [
                {"kind": "message", "engine": "codex", "content": "first"},
                {"kind": "message", "content": "missing engine"},
                {"kind": "error", "engine": "codex", "error": "boom"},
            ]
        },
        stderr="stderr tail",
    )

    assert trace == "first\n\nboom\n\n[stderr]\nstderr tail"


def test_load_subagent_execution_trace_prefers_session_event_stream_over_cached_trace(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Trace source priority")
    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="completed",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    base = workspace.task_dir(task) / ref.path
    base.mkdir(parents=True)
    (base / "execution_trace.md").write_text("cached file trace", encoding="utf-8")
    subagent_artifacts(workspace, task.id, ref.id).save(
        event_stream=SubagentEventStreamPayload({
            "events": [
                {
                    "kind": "message",
                    "engine": "codex",
                    "content": "sqlite event trace",
                }
            ]
        }),
    )

    trace = execution_trace_renderer().load_for_subagent(workspace, task, ref, active=False)

    assert trace is not None
    assert trace.text == "sqlite event trace"
    assert trace.source == "subagent_sessions:event_stream"
    assert not trace.cached_final_snapshot


def test_load_subagent_execution_trace_keeps_cached_trace_as_legacy_fallback(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Trace file fallback")
    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="completed",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    base = workspace.task_dir(task) / ref.path
    base.mkdir(parents=True)
    (base / "execution_trace.md").write_text("cached file trace", encoding="utf-8")

    trace = execution_trace_renderer().load_for_subagent(workspace, task, ref, active=False)

    assert trace is not None
    assert trace.text == "cached file trace"
    assert trace.source == base / "execution_trace.md"
    assert trace.cached_final_snapshot


def test_load_subagent_execution_trace_uses_runtime_snippet_when_artifacts_are_missing(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = WorkspaceTasks(workspace).create( title="Trace runtime fallback")
    ref = RuntimeSubagentState(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
        started_at=utcnow(),
        updated_at=utcnow(),
        execution_trace_snippet="live snippet\n",
    )

    trace = execution_trace_renderer().load_for_subagent(workspace, task, ref, active=True, runtime_state=ref)

    assert trace is not None
    assert trace.text == "live snippet"
    assert trace.source == "runtime:execution_trace_snippet"
