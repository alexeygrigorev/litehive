from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heru.base import CLIExecutionResult
from litehive.domain.agent import EngineFailure, SubagentResult
from litehive.domain.reports import StageReport, TaskActivityEntry
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.agents.manager import SubagentStartupError
from litehive.lifecycle.heru_factory import HeruEngineAdapter, _latest_verdict_after
from litehive.lifecycle.nodes.agent import AgentVerdict, NudgeRequired, TransientError, UnrecoverableError
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.sessions import Session
from litehive.lifecycle.types import PipelineMode
from litehive.tasks.paths import task_dir
from litehive.tasks.activity import load_task_activity
from litehive.tasks.reports import append_activity_entry, load_stage_reports
from heru.types import RuntimeEngineContinuation, SubagentRef


class _StubManager:
    last_init: tuple[Path, Path] | None = None
    last_kwargs: dict[str, object] | None = None

    def __init__(self, workspace_root, *, execution_root=None):
        self.workspace_root = workspace_root
        self.execution_root = execution_root
        _StubManager.last_init = (Path(workspace_root), Path(execution_root))

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
            execution=None,
            transcript="",
            exit_code=0,
            continuation=RuntimeEngineContinuation(session_id="codex-thread-123"),
        )


def test_heru_engine_adapter_updates_session_from_subagent_result_continuation(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="resume", goal="keep continuation")
    session = Session()
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", tmp_path)

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    verdict = adapter.run_turn(
        session,
        {
            "task_id": task.id,
            "stage": "implementing",
            "role": "swe",
            "pipeline_mode": "full",
            "instructions": [],
        },
        state,
    )

    assert verdict.outcome == "pass"
    assert session.engine_session_id == "codex-thread-123"
    assert session.turn_count == 1


def test_heru_engine_adapter_passes_resume_session_id_to_subagent_manager(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="resume", goal="reuse continuation")
    session = Session(engine_session_id="codex-thread-123")
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", tmp_path)

    _StubManager.last_kwargs = None
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    adapter.run_turn(
        session,
        {
            "task_id": task.id,
            "stage": "implementing",
            "role": "swe",
            "pipeline_mode": "full",
            "instructions": [],
        },
        state,
    )

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
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter(engine_name, tmp_path)

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
                execution=None,
                transcript="",
                exit_code=0,
                continuation=continuation,
            )

    _StubManager.last_kwargs = None
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _EngineSpecificStubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    verdict = adapter.run_turn(session, _heru_prompt(task.id), state)

    assert verdict.outcome == "pass"
    assert _StubManager.last_kwargs is not None
    assert _StubManager.last_kwargs["engine_name"] == engine_name
    assert session.engine_session_id == continuation.resume_id
    assert session.turn_count == 1


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
                execution=None,
                transcript="",
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
            execution=None,
            transcript="",
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


def _heru_prompt(task_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "stage": "implementing",
        "role": "swe",
        "pipeline_mode": "full",
        "instructions": [],
    }


def _subagent_result(
    engine_name: str,
    *,
    subagent_id: str,
    status: str,
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
        execution=None,
        transcript="",
        exit_code=exit_code,
        failure=failure,
        continuation=continuation,
    )


def _recovery_prompt(task_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "stage": "recovering",
        "role": "recovery",
        "pipeline_mode": "full",
        "instructions": [],
    }


@pytest.fixture(autouse=True)
def _reset_stub_manager_state() -> None:
    _StubManager.last_init = None
    _StubManager.last_kwargs = None
    _TimeoutThenResumeManager.calls = 0
    _ScriptedManager.calls = 0
    _ScriptedManager.last_kwargs = []


def test_heru_engine_adapter_launches_direct_recovery_turn_on_pre_start_subagent_failure(
    tmp_path,
    monkeypatch,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="direct recovery handoff", goal="recover startup failures")
    session = Session()
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", tmp_path)
    captured: dict[str, object] = {}

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
            captured["prompt"] = prompt
            captured["cwd"] = cwd
            captured["extra_env"] = extra_env
            append_activity_entry(
                tmp_path,
                task,
                TaskActivityEntry(
                    role="recovery",
                    stage="recovering",
                    verdict="resume",
                    target_stage="implementing",
                    message="repaired the startup path",
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
        "LITEHIVE_STAGE": "recovering",
    }
    assert captured["cwd"] == tmp_path
    assert "Role: recovery" in captured["prompt"]
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
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", tmp_path)
    captured: dict[str, object] = {}

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
            captured["prompt"] = prompt
            captured["cwd"] = cwd
            captured["extra_env"] = extra_env
            append_activity_entry(
                tmp_path,
                task,
                TaskActivityEntry(
                    role="recovery",
                    stage="recovering",
                    verdict="resume",
                    target_stage="implementing",
                    message="repaired missing engine configuration",
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

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())
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
        "LITEHIVE_STAGE": "recovering",
    }
    assert captured["cwd"] == tmp_path
    assert "Role: recovery" in captured["prompt"]
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
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", tmp_path)
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

    monkeypatch.setattr("litehive.agents.manager.get_engine", lambda _: FakeEngine())
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
        stage="recovering",
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
    adapter = HeruEngineAdapter("codex", tmp_path)
    captured: dict[str, object] = {}

    class FakeCodexAdapter:
        def run(
            self,
            prompt: str,
            cwd: Path,
            model: str | None = None,
            *,
            extra_env: dict[str, str] | None = None,
        ) -> CLIExecutionResult:
            del model, extra_env
            captured["prompt"] = prompt
            append_activity_entry(
                tmp_path,
                task,
                TaskActivityEntry(
                    role="recovery",
                    stage="recovering",
                    verdict="resume",
                    target_stage="testing",
                    message="fixed the runner startup path",
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
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", tmp_path)

    _StubManager.last_kwargs = None
    _TimeoutThenResumeManager.calls = 0
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _TimeoutThenResumeManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    with pytest.raises(TransientError, match="transient timeout"):
        adapter.run_turn(
            session,
            {
                "task_id": task.id,
                "stage": "implementing",
                "role": "swe",
                "pipeline_mode": "full",
                "instructions": [],
            },
            state,
        )

    assert session.engine_session_id == "codex-thread-123"
    assert session.turn_count == 0

    verdict = adapter.run_turn(
        session,
        {
            "task_id": task.id,
            "stage": "implementing",
            "role": "swe",
            "pipeline_mode": "full",
            "instructions": [],
        },
        state,
    )

    assert verdict.outcome == "pass"
    assert _StubManager.last_kwargs is not None
    assert _StubManager.last_kwargs["resume_session_id"] == "codex-thread-123"
    assert session.turn_count == 1


@pytest.mark.parametrize(
    ("engine_name", "continuation", "expected_resume_session_id"),
    [
        ("codex", RuntimeEngineContinuation(thread_id="codex-thread-123"), "codex-thread-123"),
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
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter(engine_name, tmp_path)

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
        "litehive.lifecycle.heru_factory._latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    verdict = adapter.run_turn(session, _heru_prompt(task.id), state)

    assert verdict.outcome == "pass"
    assert _ScriptedManager.calls == 2
    assert _ScriptedManager.last_kwargs[0]["resume_session_id"] is None
    assert _ScriptedManager.last_kwargs[1]["resume_session_id"] == expected_resume_session_id
    assert isinstance(_ScriptedManager.last_kwargs[1]["prompt"], str)
    assert _ScriptedManager.last_kwargs[1]["prompt"].startswith(HeruEngineAdapter._CRASH_RESUME_PROMPT_PREFIX)
    assert session.engine_session_id == continuation.resume_id
    assert session.turn_count == 1


def test_heru_engine_adapter_crash_resume_requires_fresh_resume_id(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="resume id required", goal="only fresh continuation can trigger crash resume")
    session = Session(engine_session_id="existing-session")
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("gemini", tmp_path)

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
    monkeypatch.setattr("litehive.lifecycle.heru_factory._latest_verdict_after", lambda *args, **kwargs: None)

    with pytest.raises(NudgeRequired, match="without a litehive report submission"):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert _ScriptedManager.calls == 1
    assert _ScriptedManager.last_kwargs[0]["resume_session_id"] == "existing-session"


def test_heru_engine_adapter_only_attempts_crash_resume_once(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="single crash resume", goal="resume at most once per crash")
    session = Session()
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("opencode", tmp_path)
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
    monkeypatch.setattr("litehive.lifecycle.heru_factory._latest_verdict_after", lambda *args, **kwargs: None)

    with pytest.raises(NudgeRequired, match="without a litehive report submission"):
        adapter.run_turn(session, _heru_prompt(task.id), state)

    assert _ScriptedManager.calls == 2
    assert _ScriptedManager.last_kwargs[1]["resume_session_id"] == continuation.resume_id
    assert session.engine_session_id == continuation.resume_id


def test_heru_engine_adapter_runs_subagent_in_task_worktree(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task, save_task

    task = create_task(tmp_path, title="worktree", goal="use execution root")
    worktree = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree.mkdir(parents=True)
    task.runtime.git.worktree_path = str(worktree)
    save_task(tmp_path, task)

    session = Session()
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", tmp_path)

    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    adapter.run_turn(
        session,
        {
            "task_id": task.id,
            "stage": "implementing",
            "role": "swe",
            "pipeline_mode": "full",
            "instructions": [],
        },
        state,
    )

    assert _StubManager.last_init == (tmp_path.resolve(), worktree.resolve())


def test_heru_engine_adapter_passes_selected_model_to_subagent_manager(
    tmp_path,
    monkeypatch,
) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="model handoff", goal="use configured model")
    session = Session()
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("goz", tmp_path).with_model("goz-preview-model")

    _StubManager.last_kwargs = None
    monkeypatch.setattr("litehive.lifecycle.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._latest_verdict_after",
        lambda *args, **kwargs: AgentVerdict(outcome="pass", reason="ok"),
    )

    adapter.run_turn(
        session,
        {
            "task_id": task.id,
            "stage": "implementing",
            "role": "swe",
            "pipeline_mode": "full",
            "instructions": [],
        },
        state,
    )

    assert _StubManager.last_kwargs is not None
    assert _StubManager.last_kwargs["model"] == "goz-preview-model"


def test_latest_verdict_after_allows_clean_implementing_noop(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="empty pass")
    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="pass",
            message="implemented nothing",
            files_changed=[],
        ),
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._execution_checkout_status",
        lambda workspace_root, task: (tmp_path, []),
    )

    verdict = _latest_verdict_after(
        tmp_path,
        task.id,
        "implementing",
        datetime.now(UTC) - timedelta(minutes=1),
    )

    assert verdict is not None
    assert verdict.outcome == "pass"


def test_latest_verdict_after_rewrites_hallucinated_implementing_pass(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="hallucinated pass")
    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="pass",
            message="implemented foo.py",
            files_changed=["foo.py"],
        ),
    )
    record = StageReport(
        task_id=task.id,
        stage="implementing",
        verdict="pass",
        source="agent",
        summary="implemented foo.py",
        feedback="implemented foo.py",
        submitted_via_cli=True,
        files_changed=["foo.py"],
    )
    from litehive.tasks.reports import record_stage_report

    record_stage_report(tmp_path, task, record)
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._execution_checkout_status",
        lambda workspace_root, task: (tmp_path, []),
    )

    verdict = _latest_verdict_after(
        tmp_path,
        task.id,
        "implementing",
        datetime.now(UTC) - timedelta(minutes=1),
    )

    assert verdict is not None
    assert verdict.outcome == "reject"
    assert verdict.source == "guard"
    assert verdict.metadata["reason_code"] == "hallucinated_completion"

    comments = load_task_activity(tmp_path, task)
    assert len(comments) == 1
    assert comments[0].verdict == "reject"
    assert "[retracted - filesystem check shows no changes landed]" in comments[0].message
    assert "reason_code: hallucinated_completion" in comments[0].message
    assert "git_status_porcelain: clean" in comments[0].message

    reports = load_stage_reports(tmp_path, task, stage="implementing")
    assert len(reports) == 1
    assert reports[0].verdict == "fail"
    assert reports[0].failure_classification == "hallucinated_completion"
    assert reports[0].outcome_reason_code == "hallucinated_completion"
    assert reports[0].failure_diagnostics["claimed_files_changed"] == ["foo.py"]

    journal = (task_dir(tmp_path, task) / "journal.md").read_text(encoding="utf-8")
    assert "Rejected implementing pass as hallucinated completion." in journal
    assert "`git status --porcelain`" in journal


def test_latest_verdict_after_allows_real_implementing_pass(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="real pass")
    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="pass",
            message="implemented change",
            files_changed=["foo.py"],
        ),
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._execution_checkout_status",
        lambda workspace_root, task: (tmp_path, [" M foo.py"]),
    )

    verdict = _latest_verdict_after(
        tmp_path,
        task.id,
        "implementing",
        datetime.now(UTC) - timedelta(minutes=1),
    )

    assert verdict is not None
    assert verdict.outcome == "pass"


def test_latest_verdict_after_includes_retry_summary_metadata(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="retry summary")
    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(
            role="swe",
            stage="implementing",
            verdict="pass",
            message=(
                "AC1: `uv run pytest -q tests/lifecycle/test_prompt_serializer.py` -> 8 passed\n"
                "AC2: `uv run ruff check --select E402,F401 litehive tests` -> all checks passed"
            ),
            files_changed=[
                "litehive/lifecycle/prompt_serializer.py",
                "tests/lifecycle/test_prompt_serializer.py",
            ],
        ),
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._execution_checkout_status",
        lambda workspace_root, task: (tmp_path, [" M litehive/lifecycle/prompt_serializer.py"]),
    )

    verdict = _latest_verdict_after(
        tmp_path,
        task.id,
        "implementing",
        datetime.now(UTC) - timedelta(minutes=1),
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
        tmp_path,
        task,
        TaskActivityEntry(
            role="recovery",
            stage="recovering",
            target_stage="testing",
            verdict="resume",
            message="fixed the runner bug",
        ),
    )

    verdict = _latest_verdict_after(
        tmp_path,
        task.id,
        "recovering",
        datetime.now(UTC) - timedelta(minutes=1),
    )

    assert verdict is not None
    assert verdict.outcome == "resume"
    assert verdict.reason == "fixed the runner bug"
    assert verdict.metadata["target_stage"] == "testing"


def test_latest_verdict_after_preserves_recovery_advance_target_stage(tmp_path) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="recovery advance target stage")
    append_activity_entry(
        tmp_path,
        task,
        TaskActivityEntry(
            role="recovery",
            stage="recovering",
            target_stage="accepting",
            verdict="advance",
            message="skip ahead to acceptance",
        ),
    )

    verdict = _latest_verdict_after(
        tmp_path,
        task.id,
        "recovering",
        datetime.now(UTC) - timedelta(minutes=1),
    )

    assert verdict is not None
    assert verdict.outcome == "advance"
    assert verdict.metadata["target_stage"] == "accepting"
