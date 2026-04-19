from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from litehive.domain.agent import EngineFailure, SubagentResult
from litehive.domain.reports import TaskActivityEntry
from litehive.lifecycle.heru_factory import HeruEngineAdapter, _latest_verdict_after
from litehive.lifecycle.nodes.agent import AgentVerdict, NudgeRequired, TransientError
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.sessions import Session
from litehive.lifecycle.types import PipelineMode
from litehive.tasks.reports import append_activity_entry
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


@pytest.fixture(autouse=True)
def _reset_stub_manager_state() -> None:
    _StubManager.last_init = None
    _StubManager.last_kwargs = None
    _TimeoutThenResumeManager.calls = 0


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


def test_latest_verdict_after_rejects_empty_implementing_pass(tmp_path, monkeypatch) -> None:
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
        ),
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._execution_checkout_has_changes",
        lambda workspace_root, task_id: False,
    )

    verdict = _latest_verdict_after(
        tmp_path,
        task.id,
        "implementing",
        datetime.now(UTC) - timedelta(minutes=1),
    )

    assert verdict is not None
    assert verdict.outcome == "reject"
    assert "execution checkout is clean" in verdict.reason


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
        ),
    )
    monkeypatch.setattr(
        "litehive.lifecycle.heru_factory._execution_checkout_has_changes",
        lambda workspace_root, task_id: True,
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
        "litehive.lifecycle.heru_factory._execution_checkout_has_changes",
        lambda workspace_root, task_id: True,
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
