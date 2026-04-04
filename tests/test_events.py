"""Tests for append-only event persistence and tail-friendly session logs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from litehive.config import ensure_workspace
from litehive.events import append_event, append_session_log, ensure_session_log, read_events
from litehive.external_cli import CLIExecutionResult
from litehive.subagents import SubagentManager
from litehive.tasks import create_task, task_dir


def test_append_event_creates_jsonl(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="event test")

    e1 = append_event(tmp_path, task, "subagent_started", data={"subagent_id": "SA-0001"})
    e2 = append_event(tmp_path, task, "subagent_finished", data={"subagent_id": "SA-0001", "exit_code": 0})

    events = read_events(tmp_path, task)
    assert len(events) == 2
    assert events[0]["kind"] == "subagent_started"
    assert events[0]["task_id"] == task.id
    assert events[0]["data"]["subagent_id"] == "SA-0001"
    assert events[1]["kind"] == "subagent_finished"
    assert events[1]["data"]["exit_code"] == 0
    assert e1["kind"] == "subagent_started"
    assert e2["kind"] == "subagent_finished"


def test_read_events_empty(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="no events")
    assert read_events(tmp_path, task) == []


def test_append_session_log_creates_and_appends(tmp_path: Path) -> None:
    base = tmp_path / "subagent"
    base.mkdir()

    append_session_log(base, "stdout", "hello ")
    append_session_log(base, "stdout", "world\n")

    log_path = base / "stdout.log"
    assert log_path.exists()
    assert log_path.read_text(encoding="utf-8") == "hello world\n"


def test_append_session_log_skips_empty(tmp_path: Path) -> None:
    base = tmp_path / "subagent"
    base.mkdir()

    append_session_log(base, "stdout", "")
    assert not (base / "stdout.log").exists()


def test_ensure_session_log_creates_empty_file(tmp_path: Path) -> None:
    base = tmp_path / "subagent"
    base.mkdir()

    path = ensure_session_log(base, "stderr")
    assert path.exists()
    assert path.read_text(encoding="utf-8") == ""


def test_subagent_run_emits_lifecycle_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="event lifecycle test")
    manager = SubagentManager(tmp_path)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            if on_started is not None:
                on_started(9999)
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout=(
                    "VERDICT: PASS\n"
                    "SUMMARY: events emitted\n"
                    "FILES_CHANGED:\n"
                    "- litehive/events.py\n"
                    "TESTS_ADDED: 1\n"
                    "TESTS_PASSING: 1\n"
                    "WARNINGS:\n"
                    "- none\n"
                ),
                stderr="some err",
                pid=9999,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.stdout

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="do it")
    assert result.ref.status == "completed"

    events = read_events(tmp_path, task)
    kinds = [e["kind"] for e in events]
    assert "subagent_started" in kinds
    assert "subagent_pid" in kinds
    assert "subagent_finished" in kinds

    started = next(e for e in events if e["kind"] == "subagent_started")
    assert started["data"]["subagent_id"] == "SA-0001"
    assert started["data"]["role"] == "swe"
    assert started["data"]["engine"] == "codex"

    pid_event = next(e for e in events if e["kind"] == "subagent_pid")
    assert pid_event["data"]["pid"] == 9999

    finished = next(e for e in events if e["kind"] == "subagent_finished")
    assert finished["data"]["status"] == "completed"
    assert finished["data"]["exit_code"] == 0


def test_subagent_run_creates_tail_friendly_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="tail-friendly log test")
    manager = SubagentManager(tmp_path)

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            on_started=None,
        ) -> CLIExecutionResult:
            if on_started is not None:
                on_started(1234)
            # Verify empty logs exist before execution finishes (tail-friendly)
            base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
            assert (base / "stdout.log").exists(), "stdout.log should exist at start"
            assert (base / "stderr.log").exists(), "stderr.log should exist at start"
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="output line 1\noutput line 2\n",
                stderr="err line\n",
                pid=1234,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.stdout

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="do it")
    assert result.ref.status == "completed"

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    stdout_log = base / "stdout.log"
    stderr_log = base / "stderr.log"

    assert stdout_log.exists()
    assert stderr_log.exists()
    assert stdout_log.read_text(encoding="utf-8") == "output line 1\noutput line 2\n"
    assert stderr_log.read_text(encoding="utf-8") == "err line\n"


def test_subagent_live_progress_appends_stream_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="stream delta test")
    manager = SubagentManager(tmp_path)

    progress_calls: list[str] = []

    class FakeEngine:
        name = "codex"
        binary = "codex"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            on_update=None,
            on_started=None,
        ) -> CLIExecutionResult:
            if on_started is not None:
                on_started(5555)
            # Simulate incremental progress updates
            partial1 = CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="chunk1\n",
                stderr="",
                pid=5555,
            )
            if on_update:
                on_update(partial1)
            progress_calls.append("p1")

            partial2 = CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="chunk1\nchunk2\n",
                stderr="err1\n",
                pid=5555,
            )
            if on_update:
                on_update(partial2)
            progress_calls.append("p2")

            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="chunk1\nchunk2\nchunk3\n",
                stderr="err1\nerr2\n",
                pid=5555,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.stdout

    monkeypatch.setattr("litehive.subagents.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="stream it")
    assert result.ref.status == "completed"
    assert len(progress_calls) == 2

    base = task_dir(tmp_path, task) / "subagents" / "SA-0001-swe"
    stdout_log = base / "stdout.log"
    stderr_log = base / "stderr.log"

    # Append-only logs should have the full content without duplication
    assert stdout_log.read_text(encoding="utf-8") == "chunk1\nchunk2\nchunk3\n"
    assert stderr_log.read_text(encoding="utf-8") == "err1\nerr2\n"

    # Events should include progress events
    events = read_events(tmp_path, task)
    kinds = [e["kind"] for e in events]
    assert kinds.count("subagent_progress") == 2


def test_events_jsonl_is_valid_per_line(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="jsonl validity")

    for i in range(5):
        append_event(tmp_path, task, f"test_event_{i}", data={"seq": i})

    path = task_dir(tmp_path, task) / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert parsed["kind"] == f"test_event_{i}"
        assert parsed["data"]["seq"] == i
