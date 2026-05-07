from pathlib import Path
from typing import Any

from heru.base import CLIExecutionResult

from litehive.agents.callbacks import SubagentRunCallbacks
from litehive.domain.runtime import Subagent
from litehive.domain.task import TaskRecord


def _subagent() -> Subagent:
    return Subagent(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
    )


def _execution(pid: int | None = 4242) -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=Path("/tmp"),
        exit_code=0,
        stdout="partial",
        stderr="",
        pid=pid,
    )


def test_subagent_run_callbacks_record_started_pid_without_progress_writer() -> None:
    task = TaskRecord(id="T-0001", slug="sample", title="Sample")
    ref = _subagent()
    recorded: list[tuple[str, int | None]] = []

    class Sessions:
        def record_subagent_pid(self, task_arg: TaskRecord, ref_arg: Subagent, pid: int | None) -> None:
            assert task_arg is task
            assert ref_arg is ref
            recorded.append((ref_arg.id, pid))

    class ProgressWriter:
        def write_session_progress(
            self,
            task_arg: TaskRecord,
            base: Path,
            ref_arg: Subagent,
            prompt: str,
            execution_arg: CLIExecutionResult,
        ) -> None:
            raise AssertionError("on_started should not write progress")

    callbacks = SubagentRunCallbacks(
        task=task,
        base=Path("/tmp/subagent"),
        ref=ref,
        prompt="do work",
        sessions=Sessions(),
        progress_writer=ProgressWriter(),
    )

    callbacks.on_started(4242)

    assert callbacks.engine_started is True
    assert recorded == [("SA-0001", 4242)]
    assert callbacks.warnings.merged_with([]) == []


def test_subagent_run_callbacks_record_progress_and_mark_started_from_pid() -> None:
    task = TaskRecord(id="T-0001", slug="sample", title="Sample")
    ref = _subagent()
    execution = _execution()
    progress_calls: list[CLIExecutionResult] = []

    class Sessions:
        def record_subagent_pid(self, task_arg: TaskRecord, ref_arg: Subagent, pid: int | None) -> None:
            raise AssertionError("on_update should not record pid directly")

    class ProgressWriter:
        def write_session_progress(
            self,
            task_arg: TaskRecord,
            base: Path,
            ref_arg: Subagent,
            prompt: str,
            execution_arg: CLIExecutionResult,
        ) -> None:
            assert task_arg is task
            assert base == Path("/tmp/subagent")
            assert ref_arg is ref
            assert prompt == "do work"
            progress_calls.append(execution_arg)

    callbacks = SubagentRunCallbacks(
        task=task,
        base=Path("/tmp/subagent"),
        ref=ref,
        prompt="do work",
        sessions=Sessions(),
        progress_writer=ProgressWriter(),
    )

    callbacks.on_update(execution)

    assert callbacks.engine_started is True
    assert progress_calls == [execution]
    assert callbacks.warnings.merged_with([]) == []


def test_subagent_run_callbacks_turn_callback_failures_into_warnings() -> None:
    task = TaskRecord(id="T-0001", slug="sample", title="Sample")
    ref = _subagent()

    class Sessions:
        def record_subagent_pid(self, task_arg: TaskRecord, ref_arg: Subagent, pid: int | None) -> None:
            raise RuntimeError("pid write failed")

    class ProgressWriter:
        def write_session_progress(self, *args: Any) -> None:
            raise RuntimeError("progress write failed")

    callbacks = SubagentRunCallbacks(
        task=task,
        base=Path("/tmp/subagent"),
        ref=ref,
        prompt="do work",
        sessions=Sessions(),
        progress_writer=ProgressWriter(),
    )

    callbacks.on_started(4242)
    callbacks.on_update(_execution(pid=None))

    assert callbacks.engine_started is True
    assert callbacks.warnings.merged_with([]) == [
        "runner start bookkeeping failed: RuntimeError: pid write failed",
        "runner progress bookkeeping failed: RuntimeError: progress write failed",
    ]
