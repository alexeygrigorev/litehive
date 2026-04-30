from pathlib import Path
import os
from typing import Any, Callable

import pytest

from heru.base import CLIExecutionResult

from litehive.agents.manager import SubagentManager, SubagentStartupError
from litehive.agents.session_store import (
    load_subagent_event_stream,
    load_subagent_report,
    load_subagent_session,
)
from litehive.config.workspace import ensure_workspace
from litehive.domain.agent import EngineFailure, SubagentInactivityTimeout
from litehive.domain.reports import TaskActivityEntry
from litehive.lifecycle.heru_factory import HeruEngineAdapter
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.paths import task_dir
from litehive.tasks.activity_rendering import append_activity_entry
from litehive.tasks.report_storage import load_stage_reports


def _fresh_codex_engine(
    *,
    run_live_override: Callable[..., CLIExecutionResult] | None = None,
):
    from heru import get_engine

    adapter_cls = type(get_engine("codex"))
    if run_live_override is None:
        return adapter_cls()

    class _TestCodexCLIAdapter(adapter_cls):
        def run_live(self, *args: Any, **kwargs: Any) -> CLIExecutionResult:
            return run_live_override(*args, **kwargs)

    return _TestCodexCLIAdapter()


def test_subagent_manager_passes_workspace_root_in_extra_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    execution_root = tmp_path / "other-project"
    execution_root.mkdir()
    task = create_task(tmp_path, title="Pass workspace root")
    manager = SubagentManager(tmp_path, execution_root=execution_root)
    captured: dict[str, object] = {}

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
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            captured["cwd"] = cwd
            captured["extra_env"] = extra_env
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="VERDICT: PASS\nSUMMARY: ok",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert captured["cwd"] == execution_root
    assert captured["extra_env"]["LITEHIVE_TASK_ID"] == task.id
    assert captured["extra_env"]["LITEHIVE_WORKSPACE_ROOT"] == str(tmp_path)
    assert captured["extra_env"]["LITEHIVE_SUBAGENT_ID"] == "SA-0001"
    assert captured["extra_env"]["LITEHIVE_STAGE"] == "implementing"


def test_subagent_manager_uses_runtime_current_stage_for_cli_verdict_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Use runtime stage for reports")
    task.runtime.pipeline.current_stage.stage = "grooming"
    save_task(tmp_path, task)
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
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            assert extra_env is not None
            append_activity_entry(
                tmp_path,
                task,
                TaskActivityEntry(
                    role="planner",
                    stage="grooming",
                    verdict="reject",
                    message="REJECT\n\nregroom this task",
                    files_changed=[],
                    source_subagent_id=extra_env["LITEHIVE_SUBAGENT_ID"],
                ),
            )
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="placeholder transcript",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="planner", engine_name="codex", prompt="groom it")

    report = load_subagent_report(tmp_path, task.id, result.ref.id)
    stage_reports = load_stage_reports(tmp_path, task)

    assert report["summary"] == "REJECT"
    assert report["warnings"] == []
    assert stage_reports[-1].source == "agent"
    assert stage_reports[-1].pipeline_state == "grooming"
    assert stage_reports[-1].summary == "REJECT"


def test_subagent_manager_uses_recovering_stage_for_recovery_cli_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Use recovery stage for reports")
    task.runtime.pipeline.current_stage.stage = "recovering"
    save_task(tmp_path, task)
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
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            assert extra_env is not None
            append_activity_entry(
                tmp_path,
                task,
                TaskActivityEntry(
                    role="recovery",
                    stage="recovering",
                    verdict="resume",
                    message="Resume from commit",
                    files_changed=[],
                    source_subagent_id=extra_env["LITEHIVE_SUBAGENT_ID"],
                ),
            )
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="placeholder transcript",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="recovery", engine_name="codex", prompt="recover it")

    report = load_subagent_report(tmp_path, task.id, result.ref.id)

    assert report["summary"] == "Resume from commit"
    assert report["warnings"] == []


def test_subagent_manager_consumes_unified_stdout_for_reports_and_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Consume unified stdout")
    manager = SubagentManager(tmp_path)
    captured: dict[str, object] = {}

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
            emit_unified: bool = False,
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            del prompt, model, extra_env
            captured["emit_unified"] = emit_unified
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout=(
                    '{"kind":"message","engine":"codex","sequence":0,'
                    '"role":"assistant","content":"implemented via unified events",'
                    '"timestamp":"2026-04-12T00:00:00+00:00","usage_delta":{},'
                    '"raw":{},"metadata":{}}\n'
                    '{"kind":"continuation","engine":"codex","sequence":1,'
                    '"timestamp":"2026-04-12T00:00:01+00:00",'
                    '"continuation_id":"session-42","usage_delta":{},'
                    '"raw":{},"metadata":{}}\n'
                ),
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return "fallback transcript should not be used"

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert captured["emit_unified"] is True
    assert result.execution_trace == "implemented via unified events"

    report = load_subagent_report(tmp_path, task.id, result.ref.id)
    session = load_subagent_session(tmp_path, task.id, result.ref.id)
    event_stream = load_subagent_event_stream(tmp_path, task.id, result.ref.id)

    assert "did not submit verdict" in report["summary"]
    assert session["continuation"]["session_id"] == "session-42"
    assert event_stream["event_counts"] == {"message": 1, "continuation": 1}

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert "last" + "_subagent" not in refreshed.runtime.model_dump()["execution"]
    assert result.continuation is not None
    assert result.continuation.resume_id == "session-42"
    assert HeruEngineAdapter.extract_continuation_id(result, None) == "session-42"


def test_subagent_manager_prefers_instance_run_override_over_inherited_run_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task")
    manager = SubagentManager(tmp_path)

    def fail_run_live(*args, **kwargs) -> CLIExecutionResult:  # type: ignore[no-untyped-def]
        raise AssertionError("run_live should not be used when only run is overridden")

    engine = _fresh_codex_engine(run_live_override=fail_run_live)
    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: engine)
    monkeypatch.setattr(engine, "is_available", lambda: True)

    calls: list[str] = []

    def fake_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started=None,
        **kwargs,
    ) -> CLIExecutionResult:
        calls.append("run")
        assert on_started is not None
        on_started(4242)
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
            pid=4242,
        )

    monkeypatch.setattr(engine, "run", fake_run)

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert calls == ["run"]
    assert result.failure == EngineFailure(kind="execution_limit", reason="usage limit reached")


def test_subagent_manager_prefers_bound_instance_run_override_over_inherited_run_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Fallback usage-limit task")
    manager = SubagentManager(tmp_path)

    def fail_run_live(*args, **kwargs) -> CLIExecutionResult:  # type: ignore[no-untyped-def]
        raise AssertionError("run_live should not be used when run is rebound to a custom method")

    engine = _fresh_codex_engine(run_live_override=fail_run_live)
    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: engine)
    monkeypatch.setattr(engine, "is_available", lambda: True)

    calls: list[str] = []

    def fake_run(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started=None,
        **kwargs,
    ) -> CLIExecutionResult:
        del self, prompt, model, max_turns, resume_session_id, kwargs
        calls.append("run")
        assert on_started is not None
        on_started(4242)
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
            pid=4242,
        )

    monkeypatch.setattr(engine, "run", fake_run.__get__(engine, type(engine)))

    result = manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert calls == ["run"]
    assert result.failure == EngineFailure(kind="execution_limit", reason="usage limit reached")


def test_subagent_manager_wraps_unexpected_pre_start_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Startup failure")
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
            on_started=None,
            **kwargs,
        ) -> CLIExecutionResult:
            del prompt, cwd, model, on_started, kwargs
            raise RuntimeError("clobbered heru stub")

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    with pytest.raises(SubagentStartupError, match="RuntimeError: clobbered heru stub"):
        manager.run(task, role="swe", engine_name="codex", prompt="implement it")


def test_subagent_manager_wraps_unavailable_engine_as_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Unavailable engine")
    manager = SubagentManager(tmp_path)

    class FakeEngine:
        name = "codex"
        binary = "missing-codex"

        def is_available(self) -> bool:
            return False

        def run(self, *args, **kwargs) -> CLIExecutionResult:
            del args, kwargs
            raise AssertionError("unavailable engines must not run")

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    with pytest.raises(
        SubagentStartupError,
        match="EngineError: Engine 'codex' is unavailable: missing binary 'missing-codex'",
    ):
        manager.run(task, role="swe", engine_name="codex", prompt="implement it")


def test_subagent_manager_preserves_started_run_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Started failure")
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
            on_started=None,
            **kwargs,
        ) -> CLIExecutionResult:
            del prompt, cwd, model, kwargs
            assert on_started is not None
            on_started(4242)
            raise RuntimeError("started run exploded")

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    with pytest.raises(RuntimeError, match="started run exploded"):
        manager.run(task, role="swe", engine_name="codex", prompt="implement it")


def test_subagent_manager_kills_stale_live_codex_output_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    manager = SubagentManager(tmp_path)
    manager.config.subagent_inactivity_timeout_seconds = 1.0
    base = tmp_path / "subagent"
    base.mkdir()
    stdout_path = base / "stdout.txt"
    stdout_path.write_text("partial output", encoding="utf-8")
    stale_time = stdout_path.stat().st_mtime - 5
    os.utime(stdout_path, (stale_time, stale_time))

    killed: list[int] = []
    monkeypatch.setattr(manager, "terminate_stale_pid", killed.append)

    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=tmp_path,
        exit_code=0,
        stdout="partial output",
        stderr="",
        pid=4242,
    )

    with pytest.raises(SubagentInactivityTimeout, match="1s without new stdout"):
        manager.check_stdout_inactivity(base, "codex", execution)

    assert killed == [4242]


def test_subagent_manager_enforces_300s_inactivity_timeout_for_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Live inactivity timeout")
    manager = SubagentManager(tmp_path)
    manager.config.subagent_inactivity_timeout_seconds = 123.0
    captured: dict[str, object] = {}

    class FakeEngine:
        name = "opencode"
        binary = "opencode"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            resume_session_id: str | None = None,
            on_started=None,
            on_update=None,
            inactivity_timeout_seconds: float = 0,
            extra_env: dict[str, str] | None = None,
            emit_unified: bool = False,
        ) -> CLIExecutionResult:
            del prompt, model, max_turns, resume_session_id, on_update, extra_env
            captured["emit_unified"] = emit_unified
            captured["inactivity_timeout_seconds"] = inactivity_timeout_seconds
            if on_started is not None:
                on_started(4242)
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout="plain transcript",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    engine = FakeEngine()
    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: engine)

    manager.run(task, role="swe", engine_name="opencode", prompt="implement it")

    assert captured["emit_unified"] is True
    assert captured["inactivity_timeout_seconds"] == 300.0


def test_subagent_manager_omits_model_override_when_resuming_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume opencode without model")
    manager = SubagentManager(tmp_path)
    captured: dict[str, object] = {}

    class FakeEngine:
        name = "opencode"
        binary = "opencode"

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            resume_session_id: str | None = None,
            on_started=None,
            on_update=None,
            extra_env: dict[str, str] | None = None,
            emit_unified: bool = False,
            **kwargs,
        ) -> CLIExecutionResult:
            del prompt, on_update, extra_env, emit_unified, kwargs
            captured["model"] = model
            captured["resume_session_id"] = resume_session_id
            if on_started is not None:
                on_started(4242)
            return CLIExecutionResult(
                adapter="opencode",
                argv=("opencode", "run"),
                cwd=cwd,
                exit_code=0,
                stdout="plain transcript",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())

    manager.run(
        task,
        role="swe",
        engine_name="opencode",
        prompt="submit the verdict",
        model="zai-coding-plan/glm-5-turbo",
        resume_session_id="opencode-session-123",
    )

    assert captured["model"] is None
    assert captured["resume_session_id"] == "opencode-session-123"


def test_subagent_manager_preserves_workspace_timeout_for_non_opencode_live_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Live inactivity timeout")
    manager = SubagentManager(tmp_path)
    manager.config.subagent_inactivity_timeout_seconds = 123.0
    captured: dict[str, object] = {}

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
            max_turns: int | None = None,
            resume_session_id: str | None = None,
            on_started=None,
            on_update=None,
            inactivity_timeout_seconds: float = 0,
            extra_env: dict[str, str] | None = None,
            emit_unified: bool = False,
        ) -> CLIExecutionResult:
            del prompt, model, max_turns, resume_session_id, on_update, extra_env
            captured["emit_unified"] = emit_unified
            captured["inactivity_timeout_seconds"] = inactivity_timeout_seconds
            if on_started is not None:
                on_started(4242)
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="plain transcript",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    engine = FakeEngine()
    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: engine)

    manager.run(task, role="swe", engine_name="codex", prompt="implement it")

    assert captured["emit_unified"] is True
    assert captured["inactivity_timeout_seconds"] == 123.0


def test_subagent_manager_survives_nonfatal_start_callback_failure_for_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Planner start callback failure")
    task.runtime.pipeline.current_stage.stage = "grooming"
    save_task(tmp_path, task)
    manager = SubagentManager(tmp_path)

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
            max_turns: int | None = None,
            resume_session_id: str | None = None,
            on_started=None,
            on_update=None,
            inactivity_timeout_seconds: float = 0,
            extra_env: dict[str, str] | None = None,
            emit_unified: bool = False,
        ) -> CLIExecutionResult:
            del (
                prompt,
                model,
                max_turns,
                resume_session_id,
                on_update,
                inactivity_timeout_seconds,
                extra_env,
                emit_unified,
            )
            if on_started is not None:
                on_started(4242)
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="planner output",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    def fail_record_pid(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("pid bookkeeping failed")

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())
    monkeypatch.setattr(manager, "record_subagent_pid", fail_record_pid)

    result = manager.run(task, role="planner", engine_name="codex", prompt="groom it")

    assert result.ref.status == "completed"
    session = load_subagent_session(tmp_path, task.id, result.ref.id)
    report = load_subagent_report(tmp_path, task.id, result.ref.id)

    assert session["pid"] == 4242
    assert any(
        "runner start bookkeeping failed: RuntimeError: pid bookkeeping failed" in warning
        for warning in report["warnings"]
    )


def test_subagent_manager_survives_nonfatal_progress_callback_failure_for_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Planner progress callback failure")
    task.runtime.pipeline.current_stage.stage = "grooming"
    save_task(tmp_path, task)
    manager = SubagentManager(tmp_path)

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
            max_turns: int | None = None,
            resume_session_id: str | None = None,
            on_started=None,
            on_update=None,
            inactivity_timeout_seconds: float = 0,
            extra_env: dict[str, str] | None = None,
            emit_unified: bool = False,
        ) -> CLIExecutionResult:
            del (
                prompt,
                model,
                max_turns,
                resume_session_id,
                inactivity_timeout_seconds,
                extra_env,
                emit_unified,
            )
            if on_started is not None:
                on_started(4242)
            partial = CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="partial planner output",
                stderr="",
                pid=4242,
            )
            if on_update is not None:
                on_update(partial)
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="final planner output",
                stderr="",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    def fail_progress(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("progress bookkeeping failed")

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())
    monkeypatch.setattr(manager, "write_session_progress", fail_progress)

    result = manager.run(task, role="planner", engine_name="codex", prompt="groom it")

    assert result.ref.status == "completed"
    session = load_subagent_session(tmp_path, task.id, result.ref.id)
    report = load_subagent_report(tmp_path, task.id, result.ref.id)

    assert session["pid"] == 4242
    assert any(
        "runner progress bookkeeping failed: RuntimeError: progress bookkeeping failed" in warning
        for warning in report["warnings"]
    )


@pytest.mark.parametrize(
    ("engine_name", "configured_timeout_seconds", "expected_timeout_seconds"),
    [
        ("opencode", 123.0, 300.0),
        ("gemini", 123.0, 123.0),
    ],
)
def test_subagent_manager_classifies_completed_inactivity_timeout_as_retryable_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine_name: str,
    configured_timeout_seconds: float,
    expected_timeout_seconds: float,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title=f"Retry stalled {engine_name} run")
    manager = SubagentManager(tmp_path)
    manager.config.subagent_inactivity_timeout_seconds = configured_timeout_seconds
    captured: dict[str, float] = {}

    class FakeEngine:
        name = engine_name
        binary = engine_name

        def is_available(self) -> bool:
            return True

        def run_live(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            max_turns: int | None = None,
            resume_session_id: str | None = None,
            on_started=None,
            on_update=None,
            inactivity_timeout_seconds: float = 0,
            extra_env: dict[str, str] | None = None,
            emit_unified: bool = False,
        ) -> CLIExecutionResult:
            del prompt, model, max_turns, resume_session_id, on_update, extra_env, emit_unified
            captured["inactivity_timeout_seconds"] = inactivity_timeout_seconds
            if on_started is not None:
                on_started(4242)
            rendered_timeout = f"{expected_timeout_seconds:g}"
            return CLIExecutionResult(
                adapter=engine_name,
                argv=(engine_name, "run"),
                cwd=cwd,
                exit_code=-15,
                stdout="still working",
                stderr=f"\n[litehive] Process killed after {rendered_timeout}s of inactivity.\n",
                pid=4242,
            )

        def render_transcript(self, execution: CLIExecutionResult) -> str:
            return execution.transcript

    engine = FakeEngine()
    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: engine)

    result = manager.run(task, role="swe", engine_name=engine_name, prompt="implement it")

    assert result.failure == EngineFailure(
        kind="retryable_execution_error",
        reason="transient timeout",
        classification="timeout",
    )
    assert captured["inactivity_timeout_seconds"] == expected_timeout_seconds
    assert result.exit_code == 124
    assert result.execution is not None
    assert result.execution.exit_code == 124
    assert f"[litehive] Process killed after {expected_timeout_seconds:g}s of inactivity." in result.execution.stderr
    assert f"{expected_timeout_seconds:g}s without new stdout" in result.execution.stderr
    assert result.ref.status == "failed"

    session = load_subagent_session(tmp_path, task.id, result.ref.id)
    report = load_subagent_report(tmp_path, task.id, result.ref.id)
    stderr_path = task_dir(tmp_path, task) / result.ref.path / "stderr.txt"

    assert session["exit_code"] == 124
    assert report["status"] == "failed"
    assert f"{expected_timeout_seconds:g}s without new stdout" in stderr_path.read_text(encoding="utf-8")
