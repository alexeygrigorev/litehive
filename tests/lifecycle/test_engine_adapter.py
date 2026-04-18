from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from litehive.domain.agent import EngineFailure, SubagentResult
from litehive.domain.reports import TaskActivityEntry
from litehive.lifecycle.heru_factory import HeruEngineAdapter, _latest_verdict_after
from litehive.lifecycle.nodes.agent import AgentVerdict, TransientError
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
