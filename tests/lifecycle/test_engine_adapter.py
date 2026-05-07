from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from heru.base import CLIExecutionResult
from litehive.domain.agent import EngineFailure, ExecutionTrace, SubagentId, SubagentResult
from litehive.domain.reports import SEMANTIC_REJECT_CLASSIFICATION, StageReport, TaskActivityEntry
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.agents.manager import SubagentStartupError
from litehive.domain.common import PipelineState, TaskStage
from litehive.lifecycle.heru_factory import HeruEngineAdapter, latest_verdict_after
from litehive.lifecycle.nodes.agent import AgentVerdict, NudgeRequired, TransientError, UnrecoverableError
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.prompt_types import AgentPrompt
from litehive.lifecycle.sessions import Session
from litehive.lifecycle.types import PipelineMode
from litehive.tasks.journal import render_task_journal
from litehive.workspace import Workspace
from litehive.tasks.activity_rendering import append_activity_entry
from litehive.tasks.report_storage import load_stage_reports
from heru.types import RuntimeEngineContinuation, SubagentRef, SubagentStatus

_SOURCE_SUBAGENT_ID = SubagentId("SA-0001")
_OTHER_SUBAGENT_ID = SubagentId("SA-0002")
_DIRECT_RECOVERY_SUBAGENT_ID = SubagentId("direct-recovery")


def _stub_execution(exit_code: int = 0, stdout: str = "", stderr: str = "") -> CLIExecutionResult:
    return CLIExecutionResult(
        adapter="test",
        argv=("test",),
        cwd=Path("/tmp"),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        pid=0,
    )


class _StubManager:
    last_init: tuple[Path, Path] | None = None
    last_kwargs: Any = None

    def __init__(self, execution_root: Path, *, workspace: Workspace, **kwargs: Any) -> None:
        del kwargs
        self.workspace_root = workspace.root
        self.execution_root = execution_root
        _StubManager.last_init = (workspace.root, Path(execution_root))

    def run(self, task, **kwargs) -> SubagentResult:
        del task
        _StubManager.last_kwargs = dict(kwargs)
        return SubagentResult(
            ref=SubagentRef(
                id="SA-0001",
                role="swe",
                engine="codex",
                status="completed",
                path="subagents/SA-0001-swe",
            ),
            execution=_stub_execution(),
            execution_trace=ExecutionTrace.from_text(""),
            exit_code=0,
            continuation=RuntimeEngineContinuation(session_id="codex-thread-123"),
        )


def test_heru_engine_adapter_updates_session_from_subagent_result_continuation(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="resume", goal="keep continuation")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", workspace=Workspace.from_path(tmp_path))

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    verdict = adapter.run_turn(session, _heru_prompt(task.id), state)

    assert verdict.outcome == "pass"
    assert session.engine_session_id == "codex-thread-123"


def test_heru_engine_adapter_passes_resume_session_id_to_subagent_manager(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="resume", goal="reuse continuation")
    session = Session(engine_session_id="codex-thread-123")
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", workspace=Workspace.from_path(tmp_path))

    _StubManager.last_kwargs = None
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    adapter.run_turn(session, _heru_prompt(task.id), state)

    assert _StubManager.last_kwargs is not None
    assert _StubManager.last_kwargs["resume_session_id"] == "codex-thread-123"


@pytest.mark.parametrize(
    ("engine_name", "continuation"),
    [
        ("codex", RuntimeEngineContinuation(thread_id="codex-thread-123")),
        ("claude", RuntimeEngineContinuation(session_id="claude-session-123")),
        ("opencode", RuntimeEngineContinuation(session_id="opencode-session-123")),
        ("copilot", RuntimeEngineContinuation(session_id="copilot-session-123")),
        ("gemini", RuntimeEngineContinuation(session_id="gemini-session-123")),
        ("goz", RuntimeEngineContinuation(session_id="goz-session-123")),
    ],
)
def test_heru_engine_adapter_launches_all_supported_engines(
    tmp_path,
    monkeypatch,
    engine_name: str,
    continuation: RuntimeEngineContinuation,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title=f"{engine_name} launch", goal="spawn through Heru")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter(engine_name, workspace=Workspace.from_path(tmp_path))

    class _EngineSpecificStubManager(_StubManager):
        def run(self, task, **kwargs) -> SubagentResult:
            del task
            _StubManager.last_kwargs = dict(kwargs)
            return SubagentResult(
                ref=SubagentRef(
                    id="SA-0001",
                    role="swe",
                    engine=engine_name,
                    status="completed",
                    path="subagents/SA-0001-swe",
                ),
                execution=_stub_execution(),
                execution_trace=ExecutionTrace.from_text(""),
                exit_code=0,
                continuation=continuation,
            )

    _StubManager.last_kwargs = None
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _EngineSpecificStubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    verdict = adapter.run_turn(session, _heru_prompt(task.id), state)

    assert verdict.outcome == "pass"
    assert _StubManager.last_kwargs is not None
    assert _StubManager.last_kwargs["engine_name"] == engine_name
    assert session.engine_session_id == continuation.resume_id


class _TimeoutThenResumeManager(_StubManager):
    calls = 0

    def run(self, task, **kwargs) -> SubagentResult:
        del task
        _TimeoutThenResumeManager.calls += 1
        _StubManager.last_kwargs = dict(kwargs)
        if _TimeoutThenResumeManager.calls == 1:
            return SubagentResult(
                ref=SubagentRef(
                    id="SA-0001",
                    role="swe",
                    engine="codex",
                    status="failed",
                    path="subagents/SA-0001-swe",
                ),
                execution=_stub_execution(exit_code=124),
                execution_trace=ExecutionTrace.from_text(""),
                exit_code=124,
                failure=EngineFailure(
                    kind="retryable_execution_error",
                    reason="transient timeout",
                    classification="timeout",
                ),
                continuation=RuntimeEngineContinuation(session_id="codex-thread-123"),
            )
        return SubagentResult(
            ref=SubagentRef(
                id="SA-0002",
                role="swe",
                engine="codex",
                status="completed",
                path="subagents/SA-0002-swe",
            ),
            execution=_stub_execution(),
            execution_trace=ExecutionTrace.from_text(""),
            exit_code=0,
            continuation=RuntimeEngineContinuation(session_id="codex-thread-123"),
        )


class _ScriptedManager(_StubManager):
    calls = 0
    last_kwargs: list[dict[str, object]] = []
    script: list[SubagentResult] = []

    def run(self, task, **kwargs) -> SubagentResult:
        del task
        _ScriptedManager.calls += 1
        _ScriptedManager.last_kwargs.append(dict(kwargs))
        index = _ScriptedManager.calls - 1
        if index >= len(_ScriptedManager.script):
            raise AssertionError(f"unexpected extra subagent run {index + 1}")
        return _ScriptedManager.script[index]


class _StartupFailureManager(_StubManager):
    def run(self, task, **kwargs) -> SubagentResult:
        del task, kwargs
        raise SubagentStartupError(AttributeError("clobbered heru stub"))


def _heru_prompt(task_id: str) -> AgentPrompt:
    del task_id  # task identity is now sourced from the TaskState the engine adapter receives
    return AgentPrompt(
        role="swe",
        stage=PipelineState.IMPLEMENTING,
        pipeline_mode=PipelineMode.FULL,
        stage_retry=0,
        instruction_variant="fresh",
        instruction_layers=[],
        last_report={},
        last_rejection=None,
        failed_run_history=[],
        runner_hooks=[],
    )


def _subagent_result(
    engine_name: str,
    *,
    subagent_id: str,
    status: SubagentStatus,
    exit_code: int,
    continuation: RuntimeEngineContinuation | None,
    failure: EngineFailure | None = None,
) -> SubagentResult:
    return SubagentResult(
        ref=SubagentRef(
            id=subagent_id,
            role="swe",
            engine=engine_name,
            status=status,
            path=f"subagents/{subagent_id}-swe",
        ),
        execution=_stub_execution(exit_code=exit_code),
        execution_trace=ExecutionTrace.from_text(""),
        exit_code=exit_code,
        failure=failure,
        continuation=continuation,
    )


def _recovery_prompt(task_id: str) -> AgentPrompt:
    del task_id  # task identity is now sourced from the TaskState the engine adapter receives
    return AgentPrompt(
        role="recovery",
        stage=PipelineState.RECOVERING,
        pipeline_mode=PipelineMode.FULL,
        stage_retry=0,
        instruction_variant="fresh",
        instruction_layers=[],
        last_report={},
        last_rejection=None,
        failed_run_history=[],
        runner_hooks=[],
    )


@pytest.fixture(autouse=True)
def _reset_stub_manager_state() -> None:
    _StubManager.last_init = None
    _StubManager.last_kwargs = None
    _TimeoutThenResumeManager.calls = 0
    _ScriptedManager.calls = 0
    _ScriptedManager.last_kwargs = []


def test_heru_engine_adapter_runs_recovery_from_litehive_source_checkout(tmp_path, monkeypatch) -> None:
    from litehive.config.loading import load_config
    from litehive.config.model import LitehiveConfig
    from litehive.config.workspace import create_workspace
    from litehive.state.records import create_task, save_task, set_task_worktree_path

    source_repo = tmp_path / "litehive-src"
    source_repo.mkdir()
    create_workspace(tmp_path, LitehiveConfig(litehive_source_path=str(source_repo)))
    task = create_task(tmp_path, title="recovery source checkout", goal="recover from source repo")
    worktree = tmp_path / "task-checkout"
    worktree.mkdir()
    set_task_worktree_path(task, str(worktree.relative_to(tmp_path)))
    save_task(tmp_path, task)

    session = Session()
    state = TaskState(
        task_id=task.id,
        stage=PipelineState.RECOVERING,
        pipeline_mode=PipelineMode.FULL,
    )
    adapter = HeruEngineAdapter(
        "codex",
        workspace=Workspace.from_path(tmp_path),
        config=load_config(tmp_path),
    )

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="resume", reason="fixed", metadata={"target_stage": "testing"}),
    )

    verdict = adapter.run_turn(session, _recovery_prompt(task.id), state)

    assert verdict.outcome == "resume"
    assert _StubManager.last_init == (tmp_path.resolve(), source_repo.resolve())


def test_heru_engine_adapter_launches_direct_recovery_turn_on_pre_start_subagent_failure(
    tmp_path,
    monkeypatch,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="direct recovery handoff", goal="recover startup failures")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", workspace=Workspace.from_path(tmp_path))
    captured: dict[str, Any] = {}

    class FakeCodexAdapter:
        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            del model
            assert extra_env is not None
            captured["prompt"] = prompt
            captured["cwd"] = cwd
            captured["extra_env"] = extra_env
            append_activity_entry(
                Workspace.from_path(tmp_path),
                task,
                TaskActivityEntry(
                    role="recovery",
                    stage=PipelineState.RECOVERING,
                    verdict="resume",
                    target_stage="implementing",
                    message="repaired the startup path",
                    source_subagent_id=extra_env["LITEHIVE_SUBAGENT_ID"],
                ),
            )
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="",
                stderr="",
                pid=4242,
            )

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StartupFailureManager)
    monkeypatch.setattr("litehive.lifecycle.heru_factory.CodexCLIAdapter", FakeCodexAdapter)

    with pytest.raises(UnrecoverableError, match="AttributeError: clobbered heru stub"):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert captured["extra_env"] == {
        "LITEHIVE_TASK_ID": task.id,
        "LITEHIVE_WORKSPACE_ROOT": str(tmp_path),
        "LITEHIVE_AGENT_ROLE": "recovery",
        "LITEHIVE_SUBAGENT_ID": "direct-recovery",
        "LITEHIVE_STAGE": "recovering",
    }
    assert captured["cwd"] == tmp_path
    assert "You are the recovery agent." in captured["prompt"]
    assert "Stage: recovering" in captured["prompt"]
    assert "Litehive cannot start its own subagents" in captured["prompt"]
    assert "AttributeError: clobbered heru stub" in captured["prompt"]


def test_heru_engine_adapter_launches_direct_recovery_turn_when_engine_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="missing binary handoff", goal="recover unavailable engine")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", workspace=Workspace.from_path(tmp_path))
    captured: dict[str, Any] = {}

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

    class FakeCodexAdapter:
        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            del model
            assert extra_env is not None
            captured["prompt"] = prompt
            captured["cwd"] = cwd
            captured["extra_env"] = extra_env
            append_activity_entry(
                Workspace.from_path(tmp_path),
                task,
                TaskActivityEntry(
                    role="recovery",
                    stage=PipelineState.RECOVERING,
                    verdict="resume",
                    target_stage="implementing",
                    message="repaired missing engine configuration",
                    source_subagent_id=extra_env["LITEHIVE_SUBAGENT_ID"],
                ),
            )
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="",
                stderr="",
                pid=4242,
            )

    monkeypatch.setattr("litehive.agents.engine_manager.get_engine", lambda _: FakeEngine())
    monkeypatch.setattr("litehive.lifecycle.heru_factory.CodexCLIAdapter", FakeCodexAdapter)

    with pytest.raises(
        UnrecoverableError,
        match="EngineError: Engine 'codex' is unavailable: missing binary 'missing-codex'",
    ):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert captured["extra_env"] == {
        "LITEHIVE_TASK_ID": task.id,
        "LITEHIVE_WORKSPACE_ROOT": str(tmp_path),
        "LITEHIVE_AGENT_ROLE": "recovery",
        "LITEHIVE_SUBAGENT_ID": "direct-recovery",
        "LITEHIVE_STAGE": "recovering",
    }
    assert captured["cwd"] == tmp_path
    assert "You are the recovery agent." in captured["prompt"]
    assert "Stage: recovering" in captured["prompt"]
    assert "Litehive cannot start its own subagents" in captured["prompt"]
    assert "EngineError: Engine 'codex' is unavailable: missing binary 'missing-codex'" in captured["prompt"]


def test_heru_engine_adapter_does_not_launch_direct_recovery_after_started_run_failure(
    tmp_path,
    monkeypatch,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="started failure", goal="preserve post-start failures")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", workspace=Workspace.from_path(tmp_path))
    captured = {"called": False}

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

    class FakeCodexAdapter:
        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            del prompt, cwd, model, extra_env
            captured["called"] = True
            raise AssertionError("direct recovery must not run after the engine started")

    monkeypatch.setattr("litehive.agents.engine_manager.get_engine", lambda _: FakeEngine())
    monkeypatch.setattr("litehive.lifecycle.heru_factory.CodexCLIAdapter", FakeCodexAdapter)

    with pytest.raises(UnrecoverableError, match="RuntimeError: started run exploded"):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert captured["called"] is False


def test_heru_engine_adapter_returns_direct_recovery_verdict_during_recovering_stage(
    tmp_path,
    monkeypatch,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="recovery stage handoff", goal="return recovery verdict")
    session = Session()
    state = TaskState(
        task_id=task.id,
        stage=PipelineState.RECOVERING,
        pipeline_mode=PipelineMode.FULL,
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="implementing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="implementing-crash",
                classification="engine_crash",
            ),
            reason_code="stage_exception",
            message="implementing crashed",
        ),
    )
    adapter = HeruEngineAdapter("codex", workspace=Workspace.from_path(tmp_path))
    captured: dict[str, Any] = {}

    class FakeCodexAdapter:
        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            del model
            assert extra_env is not None
            captured["prompt"] = prompt
            append_activity_entry(
                Workspace.from_path(tmp_path),
                task,
                TaskActivityEntry(
                    role="recovery",
                    stage=PipelineState.RECOVERING,
                    verdict="resume",
                    target_stage="testing",
                    message="fixed the runner startup path",
                    source_subagent_id=extra_env["LITEHIVE_SUBAGENT_ID"],
                ),
            )
            return CLIExecutionResult(
                adapter="codex",
                argv=("codex", "exec"),
                cwd=cwd,
                exit_code=0,
                stdout="",
                stderr="",
                pid=4242,
            )

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StartupFailureManager)
    monkeypatch.setattr("litehive.lifecycle.heru_factory.CodexCLIAdapter", FakeCodexAdapter)

    verdict = adapter.run_turn(session, _recovery_prompt(task.id), state)

    assert verdict.outcome == "resume"
    assert verdict.reason == "fixed the runner startup path"
    assert verdict.metadata["target_stage"] == "testing"
    assert "origin_stage: implementing" in captured["prompt"]
    assert "AttributeError: clobbered heru stub" in captured["prompt"]


def test_heru_engine_adapter_reuses_failed_turn_continuation_on_retry(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="resume timeout", goal="reuse continuation after timeout")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", workspace=Workspace.from_path(tmp_path))

    _StubManager.last_kwargs = None
    _TimeoutThenResumeManager.calls = 0
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _TimeoutThenResumeManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    with pytest.raises(TransientError, match="transient timeout"):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert session.engine_session_id == "codex-thread-123"

    verdict = adapter.run_turn(session, _heru_prompt(task.id), state)

    assert verdict.outcome == "pass"
    assert _StubManager.last_kwargs is not None
    assert _StubManager.last_kwargs["resume_session_id"] == "codex-thread-123"


@pytest.mark.parametrize(
    ("engine_name", "continuation", "expected_resume_session_id"),
    [
        ("codex", RuntimeEngineContinuation(thread_id="codex-thread-123"), "codex-thread-123"),
        ("claude", RuntimeEngineContinuation(session_id="claude-session-123"), "claude-session-123"),
        ("gemini", RuntimeEngineContinuation(session_id="gemini-session-123"), "gemini-session-123"),
        ("opencode", RuntimeEngineContinuation(session_id="opencode-session-123"), "opencode-session-123"),
    ],
)
def test_heru_engine_adapter_retries_crash_once_with_resume_id(
    tmp_path,
    monkeypatch,
    engine_name: str,
    continuation: RuntimeEngineContinuation,
    expected_resume_session_id: str | None,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title=f"{engine_name} crash resume", goal="retry crashed run once")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter(engine_name, workspace=Workspace.from_path(tmp_path))

    _ScriptedManager.calls = 0
    _ScriptedManager.last_kwargs = []
    _ScriptedManager.script = [
        _subagent_result(
            engine_name,
            subagent_id="SA-0001",
            status="failed",
            exit_code=1,
            continuation=continuation,
        ),
        _subagent_result(
            engine_name,
            subagent_id="SA-0002",
            status="completed",
            exit_code=0,
            continuation=continuation,
        ),
    ]

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _ScriptedManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    verdict = adapter.run_turn(session, _heru_prompt(task.id), state)

    assert verdict.outcome == "pass"
    assert _ScriptedManager.calls == 2
    assert _ScriptedManager.last_kwargs[0]["resume_session_id"] is None
    assert _ScriptedManager.last_kwargs[1]["resume_session_id"] == expected_resume_session_id
    assert isinstance(_ScriptedManager.last_kwargs[1]["prompt"], str)
    assert _ScriptedManager.last_kwargs[1]["prompt"].startswith(HeruEngineAdapter.CRASH_RESUME_PROMPT_PREFIX)
    assert session.engine_session_id == continuation.resume_id


@pytest.mark.parametrize("engine_name", ["copilot", "goz"])
def test_heru_engine_adapter_skips_crash_resume_without_resume_id(
    tmp_path,
    monkeypatch,
    engine_name: str,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title=f"{engine_name} no resume id", goal="skip crash resume without continuation id")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter(engine_name, workspace=Workspace.from_path(tmp_path))

    _ScriptedManager.calls = 0
    _ScriptedManager.last_kwargs = []
    _ScriptedManager.script = [
        _subagent_result(
            engine_name,
            subagent_id="SA-0001",
            status="failed",
            exit_code=1,
            continuation=RuntimeEngineContinuation(),
        ),
    ]

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _ScriptedManager)
    monkeypatch.setattr("litehive.lifecycle.heru_factory.latest_verdict_after", lambda *args, **kwargs: None)

    with pytest.raises(NudgeRequired, match="without a litehive agent report submission"):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert _ScriptedManager.calls == 1
    assert _ScriptedManager.last_kwargs[0]["resume_session_id"] is None
    first_prompt = _ScriptedManager.last_kwargs[0]["prompt"]
    assert isinstance(first_prompt, str)
    assert not first_prompt.startswith(HeruEngineAdapter.CRASH_RESUME_PROMPT_PREFIX)
    assert session.engine_session_id is None


def test_heru_engine_adapter_crash_resume_requires_fresh_resume_id(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="resume id required", goal="only fresh continuation can trigger crash resume")
    session = Session(engine_session_id="existing-session")
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("gemini", workspace=Workspace.from_path(tmp_path))

    _ScriptedManager.calls = 0
    _ScriptedManager.last_kwargs = []
    _ScriptedManager.script = [
        _subagent_result(
            "gemini",
            subagent_id="SA-0001",
            status="failed",
            exit_code=1,
            continuation=None,
        ),
    ]

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _ScriptedManager)
    monkeypatch.setattr("litehive.lifecycle.heru_factory.latest_verdict_after", lambda *args, **kwargs: None)

    with pytest.raises(NudgeRequired, match="without a litehive agent report submission"):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert _ScriptedManager.calls == 1
    assert _ScriptedManager.last_kwargs[0]["resume_session_id"] == "existing-session"


def test_heru_engine_adapter_only_attempts_crash_resume_once(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="single crash resume", goal="resume at most once per crash")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("opencode", workspace=Workspace.from_path(tmp_path))
    continuation = RuntimeEngineContinuation(session_id="opencode-session-123")

    _ScriptedManager.calls = 0
    _ScriptedManager.last_kwargs = []
    _ScriptedManager.script = [
        _subagent_result(
            "opencode",
            subagent_id="SA-0001",
            status="failed",
            exit_code=1,
            continuation=continuation,
        ),
        _subagent_result(
            "opencode",
            subagent_id="SA-0002",
            status="failed",
            exit_code=1,
            continuation=continuation,
        ),
    ]

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _ScriptedManager)
    monkeypatch.setattr("litehive.lifecycle.heru_factory.latest_verdict_after", lambda *args, **kwargs: None)

    with pytest.raises(NudgeRequired, match="without a litehive agent report submission"):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert _ScriptedManager.calls == 2
    assert _ScriptedManager.last_kwargs[1]["resume_session_id"] == continuation.resume_id
    assert session.engine_session_id == continuation.resume_id


def test_heru_engine_adapter_runs_subagent_in_task_worktree(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task, save_task

    task = create_task(tmp_path, title="worktree", goal="use execution root")
    worktree = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree.mkdir(parents=True)
    task.runtime.pipeline.git.worktree_path = str(worktree)
    save_task(tmp_path, task)

    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", workspace=Workspace.from_path(tmp_path))

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    adapter.run_turn(session, _heru_prompt(task.id), state)

    assert _StubManager.last_init == (tmp_path.resolve(), worktree.resolve())


def test_heru_engine_adapter_passes_selected_model_to_subagent_manager(
    tmp_path,
    monkeypatch,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="model handoff", goal="use configured model")
    session = Session()
    state = TaskState(task_id=task.id, stage=PipelineState.IMPLEMENTING, pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("goz", workspace=Workspace.from_path(tmp_path)).with_model("goz-preview-model")

    _StubManager.last_kwargs = None
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    adapter.run_turn(session, _heru_prompt(task.id), state)

    assert _StubManager.last_kwargs is not None
    assert _StubManager.last_kwargs["model"] == "goz-preview-model"


def test_latest_verdict_after_allows_clean_implementing_noop(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="empty pass")
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="swe",
            stage=PipelineState.IMPLEMENTING,
            verdict="pass",
            message="implemented nothing",
            files_changed=[],
            source_subagent_id=_SOURCE_SUBAGENT_ID,
        ),
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.execution_checkout_status",
        lambda workspace, task: (tmp_path, []),
    )

    verdict = latest_verdict_after(
        Workspace.from_path(tmp_path),
        task.id,
        TaskStage.IMPLEMENTING,
        datetime.now(UTC) - timedelta(minutes=1),
        source_subagent_id=_SOURCE_SUBAGENT_ID,
    )

    assert verdict is not None
    assert verdict.outcome == "pass"


def test_latest_verdict_after_rewrites_hallucinated_implementing_pass(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="hallucinated pass")
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="swe",
            stage=PipelineState.IMPLEMENTING,
            verdict="pass",
            message="implemented foo.py",
            files_changed=["foo.py"],
            source_subagent_id=_SOURCE_SUBAGENT_ID,
        ),
    )
    record = StageReport(
        task_id=task.id,
        pipeline_state="implementing",
        verdict="pass",
        source="agent",
        summary="implemented foo.py",
        feedback="implemented foo.py",
        submitted_via_cli=True,
    )
    from litehive.tasks.report_storage import record_stage_report

    record_stage_report(Workspace.from_path(tmp_path), task, record)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.execution_checkout_status",
        lambda workspace, task: (tmp_path, []),
    )

    verdict = latest_verdict_after(
        Workspace.from_path(tmp_path),
        task.id,
        TaskStage.IMPLEMENTING,
        datetime.now(UTC) - timedelta(minutes=1),
        source_subagent_id=_SOURCE_SUBAGENT_ID,
    )

    assert verdict is not None
    assert verdict.outcome == "reject"
    assert verdict.source == "guard"
    assert verdict.metadata["reason_code"] == "hallucinated_completion"

    activity_entries = Workspace.from_path(tmp_path).task_activity(task).load()
    assert len(activity_entries) == 1
    assert activity_entries[0].verdict == "reject"
    assert "[retracted - filesystem check shows no changes landed]" in activity_entries[0].message
    assert "reason_code: hallucinated_completion" in activity_entries[0].message
    assert "git_status_porcelain: clean" in activity_entries[0].message

    reports = load_stage_reports(Workspace.from_path(tmp_path), task, pipeline_state="implementing")
    assert len(reports) == 1
    assert reports[0].verdict == "reject"
    assert reports[0].failure_classification == "hallucinated_completion"
    assert reports[0].outcome_reason_code == "hallucinated_completion"
    assert reports[0].failure_diagnostics["claimed_files_changed"] == ["foo.py"]

    journal = render_task_journal(Workspace.from_path(tmp_path), task)
    assert "Rejected implementing pass as hallucinated completion." in journal
    assert "`git status --porcelain`" in journal


def test_latest_verdict_after_allows_real_implementing_pass(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="real pass")
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="swe",
            stage=PipelineState.IMPLEMENTING,
            verdict="pass",
            message="implemented change",
            files_changed=["foo.py"],
            source_subagent_id=_SOURCE_SUBAGENT_ID,
        ),
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.execution_checkout_status",
        lambda workspace, task: (tmp_path, [" M foo.py"]),
    )

    verdict = latest_verdict_after(
        Workspace.from_path(tmp_path),
        task.id,
        TaskStage.IMPLEMENTING,
        datetime.now(UTC) - timedelta(minutes=1),
        source_subagent_id=_SOURCE_SUBAGENT_ID,
    )

    assert verdict is not None
    assert verdict.outcome == "pass"


def test_latest_verdict_after_returns_semantic_reject_classification(tmp_path) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="semantic reviewer reject")
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="reviewer",
            stage=PipelineState.ACCEPTING,
            verdict="reject",
            verdict_classification=SEMANTIC_REJECT_CLASSIFICATION,
            message="acceptance evidence is incomplete",
            source_subagent_id=_SOURCE_SUBAGENT_ID,
        ),
    )

    verdict = latest_verdict_after(
        Workspace.from_path(tmp_path),
        task.id,
        TaskStage.ACCEPTING,
        datetime.now(UTC) - timedelta(minutes=1),
        source_subagent_id=_SOURCE_SUBAGENT_ID,
    )

    assert verdict is not None
    assert verdict.outcome == "reject"
    assert verdict.classification == SEMANTIC_REJECT_CLASSIFICATION
    assert verdict.metadata["verdict_classification"] == SEMANTIC_REJECT_CLASSIFICATION


def test_latest_verdict_after_can_filter_to_source_subagent_id(tmp_path) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="source-bound verdict")
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="swe",
            stage=PipelineState.IMPLEMENTING,
            verdict="pass",
            message="wrong session",
            source_subagent_id=_OTHER_SUBAGENT_ID,
        ),
    )
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="swe",
            stage=PipelineState.IMPLEMENTING,
            verdict="reject",
            message="current session",
            source_subagent_id=_SOURCE_SUBAGENT_ID,
        ),
    )

    verdict = latest_verdict_after(
        Workspace.from_path(tmp_path),
        task.id,
        TaskStage.IMPLEMENTING,
        datetime.now(UTC) - timedelta(minutes=1),
        source_subagent_id=_SOURCE_SUBAGENT_ID,
    )

    assert verdict is not None
    assert verdict.outcome == "reject"
    assert verdict.reason == "current session"


def test_latest_verdict_after_includes_retry_summary_metadata(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="retry summary")
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="swe",
            stage=PipelineState.IMPLEMENTING,
            verdict="pass",
            message=(
                "AC1: `uv run pytest -q tests/lifecycle/test_prompt_serializer.py` -> 8 passed\n"
                "AC2: `uv run ruff check --select E402,F401 litehive tests` -> all checks passed"
            ),
            files_changed=[
                "litehive/lifecycle/prompt_serializer.py",
                "tests/lifecycle/test_prompt_serializer.py",
            ],
            source_subagent_id=_SOURCE_SUBAGENT_ID,
        ),
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory.execution_checkout_status",
        lambda workspace, task: (tmp_path, [" M litehive/lifecycle/prompt_serializer.py"]),
    )

    verdict = latest_verdict_after(
        Workspace.from_path(tmp_path),
        task.id,
        TaskStage.IMPLEMENTING,
        datetime.now(UTC) - timedelta(minutes=1),
        source_subagent_id=_SOURCE_SUBAGENT_ID,
    )

    assert verdict is not None
    assert verdict.outcome == "pass"
    assert verdict.metadata["last_report"] == {
        "changed_files": [
            "litehive/lifecycle/prompt_serializer.py",
            "tests/lifecycle/test_prompt_serializer.py",
        ],
        "test_results": [
            "AC1: `uv run pytest -q tests/lifecycle/test_prompt_serializer.py` -> 8 passed",
            "AC2: `uv run ruff check --select E402,F401 litehive tests` -> all checks passed",
        ],
    }


def test_latest_verdict_after_accepts_recovery_resume(tmp_path) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="recovery resume")
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="recovery",
            stage=PipelineState.RECOVERING,
            target_stage="testing",
            verdict="resume",
            message="fixed the runner bug",
            source_subagent_id=_DIRECT_RECOVERY_SUBAGENT_ID,
        ),
    )

    verdict = latest_verdict_after(
        Workspace.from_path(tmp_path),
        task.id,
        PipelineState.RECOVERING,
        datetime.now(UTC) - timedelta(minutes=1),
        source_subagent_id=_DIRECT_RECOVERY_SUBAGENT_ID,
    )

    assert verdict is not None
    assert verdict.outcome == "resume"
    assert verdict.reason == "fixed the runner bug"
    assert verdict.metadata["target_stage"] == "testing"


def test_latest_verdict_after_preserves_recovery_advance_target_stage(tmp_path) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="recovery advance target stage")
    append_activity_entry(
        Workspace.from_path(tmp_path),
        task,
        TaskActivityEntry(
            role="recovery",
            stage=PipelineState.RECOVERING,
            target_stage="accepting",
            verdict="advance",
            message="skip ahead to acceptance",
            source_subagent_id=_DIRECT_RECOVERY_SUBAGENT_ID,
        ),
    )

    verdict = latest_verdict_after(
        Workspace.from_path(tmp_path),
        task.id,
        PipelineState.RECOVERING,
        datetime.now(UTC) - timedelta(minutes=1),
        source_subagent_id=_DIRECT_RECOVERY_SUBAGENT_ID,
    )

    assert verdict is not None
    assert verdict.outcome == "advance"
    assert verdict.metadata["target_stage"] == "accepting"
