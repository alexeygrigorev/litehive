import json
from pathlib import Path

import pytest

from heru.base import CLIExecutionResult
from heru.types import SubagentRef

from litehive.container import build_subagent_manager
from litehive.agents.session_store import load_subagent_report, load_subagent_event_stream
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import create_workspace
from litehive.db.schema import connect_workspace_db
from litehive.recovery.execution_recovery import mark_interrupted_subagent
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.paths import task_dir
from litehive.tasks.runtime import mark_subagent_started_for_workspace
from litehive.workspace import Workspace


def test_claude_live_progress_report_uses_unified_execution_trace_for_restart_snippet(
    tmp_path: Path,
) -> None:
    create_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    workspace = Workspace.from_path(tmp_path)
    task = create_task(tmp_path, title="Claude live restart summary", auto_commit=False)
    manager = build_subagent_manager(tmp_path, execution_root=tmp_path)

    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="claude",
        status="running",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    mark_subagent_started_for_workspace(workspace, task, ref)
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=False)
    manager.sessions.write_session_start(task, base, ref, "stream partial Claude output")

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout=(
            '{"kind":"message","engine":"claude","sequence":0,'
            '"role":"assistant","content":"SUMMARY: partial Claude output",'
            '"timestamp":"2026-04-12T00:00:00+00:00","usage_delta":{},'
            '"raw":{},"metadata":{}}\n'
        ),
        stderr="",
        pid=4242,
    )

    manager.write_session_progress(task, base, ref, "stream partial Claude output", execution)

    report = load_subagent_report(workspace, task.id, "SA-0001")
    assert report["status"] == "running"
    assert "did not submit verdict" in report["summary"]
    assert report["files_changed"] == []

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    interrupted = mark_interrupted_subagent(
        workspace,
        refreshed,
        reason="runner interrupted before subagent completion",
        stage="implementing",
    )

    assert interrupted is not None
    assert "did not submit verdict" in interrupted.execution_trace_snippet

    resumed_report = load_subagent_report(workspace, task.id, "SA-0001")
    assert resumed_report["status"] == "interrupted"
    assert resumed_report["resume_stage"] == "implementing"


def test_subagent_writes_event_stream_during_live_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Event stream live progress test")
    manager = build_subagent_manager(tmp_path, execution_root=tmp_path)

    partial_stdout = (
        '{"kind":"message","engine":"opencode","sequence":0,'
        '"role":"assistant","content":"partial output",'
        '"timestamp":"2026-04-12T00:00:00+00:00","usage_delta":{},'
        '"raw":{},"metadata":{}}\n'
    )

    class FakeStreamingEngine:
        name = "opencode"
        binary = "opencode"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt,
            cwd,
            model=None,
            *,
            max_turns=None,
            on_update=None,
            inactivity_timeout_seconds=0,
            **kwargs,
        ):
            first = CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=partial_stdout,
                stderr="",
                pid=5151,
            )
            if on_update is not None:
                on_update(first)

            event_stream_data = load_subagent_event_stream(Workspace.from_path(tmp_path), task.id, "SA-0001")
            assert event_stream_data["engine"] == "opencode"
            assert event_stream_data["task_id"] == task.id
            assert len(event_stream_data["events"]) == 1

            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=(
                    partial_stdout + '{"kind":"usage","engine":"opencode","sequence":1,'
                    '"timestamp":"2026-04-12T00:00:01+00:00","usage_delta":{"total_tokens":50},'
                    '"raw":{},"metadata":{"total_tokens":50}}\n'
                ),
                stderr="",
                pid=5151,
            )

        def render_transcript(self, execution):
            return execution.transcript

    monkeypatch.setattr("litehive.agents.engine_manager.get_engine", lambda _: FakeStreamingEngine())

    result = manager.run(task, role="swe", engine_name="opencode", prompt="stream it")
    assert result.ref.status == "completed"

    event_stream_data = load_subagent_event_stream(Workspace.from_path(tmp_path), task.id, "SA-0001")
    assert len(event_stream_data["events"]) == 2
    assert event_stream_data["event_counts"] == {"message": 1, "usage": 1}


def test_subagent_skips_event_stream_when_no_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="No event stream test")
    manager = build_subagent_manager(tmp_path, execution_root=tmp_path)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(self, prompt, cwd, model=None, *, max_turns=None, on_started=None):
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="plain text output with no jsonl",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution):
            return execution.stdout

    monkeypatch.setattr("litehive.agents.engine_manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="no events")
    assert result.ref.status == "completed"

    assert load_subagent_event_stream(Workspace.from_path(tmp_path), task.id, "SA-0001") == {}


def test_subagent_event_stream_ignores_removed_timeline_payload_key(tmp_path: Path) -> None:
    create_workspace(tmp_path)
    task = create_task(tmp_path, title="Current event stream key")
    with connect_workspace_db(tmp_path) as connection:
        connection.execute(
            """
            INSERT INTO subagent_sessions (
                task_id,
                subagent_id,
                created_at,
                updated_at,
                payload
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                task.id,
                "SA-0001",
                "2026-04-30T00:00:00+00:00",
                "2026-04-30T00:00:00+00:00",
                json.dumps({"timeline": {"events": [{"kind": "message", "content": "old"}]}}),
            ),
        )

    assert load_subagent_event_stream(Workspace.from_path(tmp_path), task.id, "SA-0001") == {}
