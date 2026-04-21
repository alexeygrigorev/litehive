from pathlib import Path

import pytest

from heru.base import CLIExecutionResult
from heru.types import SubagentRef

from litehive.agents.manager import SubagentManager
from litehive.agents.session_store import load_subagent_report, load_subagent_timeline
from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.recovery.execution_recovery import mark_interrupted_subagent
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.paths import task_dir
from litehive.tasks.runtime import mark_subagent_started


def test_claude_live_progress_report_uses_adapter_summary_for_restart_snippet(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path, LitehiveConfig(default_engine="claude"))
    task = create_task(tmp_path, title="Claude live restart summary", auto_commit=False)
    manager = SubagentManager(tmp_path)

    ref = SubagentRef(
        id="SA-0001",
        role="swe",
        engine="claude",
        status="running",
        path="subagents/SA-0001-swe",
    )
    task.subagents.append(ref)
    mark_subagent_started(tmp_path, task, ref)
    save_task(tmp_path, task)

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    base.mkdir(parents=True, exist_ok=False)
    manager._write_session_start(task, base, ref, "stream partial Claude output")

    execution = CLIExecutionResult(
        adapter="claude",
        argv=("claude", "-p"),
        cwd=tmp_path,
        exit_code=0,
        stdout="\n".join(
            [
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"VERDICT: PASS\\n"}}',
                '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"SUMMARY: partial Claude output\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"FILES_CHANGED:\\n- litehive/engines.py\\n"}}',
                '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"TESTS_ADDED: 1\\nTESTS_PASSING: 1\\nWARNINGS:\\n"}}',
            ]
        ),
        stderr="",
        pid=4242,
    )

    manager._write_session_progress(task, base, ref, "stream partial Claude output", execution)

    report = load_subagent_report(tmp_path, task.id, "SA-0001")
    assert report["status"] == "running"
    assert "did not submit verdict" in report["summary"]
    assert report["files_changed"] == []

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    interrupted = mark_interrupted_subagent(
        tmp_path,
        refreshed,
        reason="runner interrupted before subagent completion",
        stage="implementing",
    )

    assert interrupted is not None
    assert "did not submit verdict" in interrupted.transcript_snippet

    resumed_report = load_subagent_report(tmp_path, task.id, "SA-0001")
    assert resumed_report["status"] == "interrupted"
    assert resumed_report["resume_stage"] == "implementing"


def test_subagent_writes_timeline_during_live_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Timeline live progress test")
    manager = SubagentManager(tmp_path)

    partial_stdout = '{"type":"text","part":{"text":"partial output"}}\n'

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

            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            timeline_data = load_subagent_timeline(tmp_path, task.id, "SA-0001")
            assert timeline_data["engine"] == "opencode"
            assert timeline_data["task_id"] == task.id
            assert len(timeline_data["events"]) == 1

            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout=partial_stdout + '{"type":"step_finish","part":{"tokens":{"total":50}}}\n',
                stderr="",
                pid=5151,
            )

        def render_transcript(self, execution):
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeStreamingEngine())

    result = manager.run(task, role="swe", engine_name="opencode", prompt="stream it")
    assert result.ref.status == "completed"

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    timeline_data = load_subagent_timeline(tmp_path, task.id, "SA-0001")
    assert len(timeline_data["events"]) == 2
    assert timeline_data["event_counts"] == {"message": 1, "usage": 1}


def test_subagent_skips_timeline_when_no_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No timeline test")
    manager = SubagentManager(tmp_path)

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

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="no events")
    assert result.ref.status == "completed"

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    assert load_subagent_timeline(tmp_path, task.id, "SA-0001") == {}
