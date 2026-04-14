from datetime import UTC, datetime, timedelta
from pathlib import Path

from litehive.domain.agent import SubagentResult
from heru.types import SubagentRef
from litehive.domain.reports import TaskThreadComment
from litehive.lifecycle.heru_factory import HeruEngineAdapter, _latest_verdict_after
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.sessions import Session
from litehive.lifecycle.types import PipelineMode
from litehive.tasks.reports import append_thread_comment
from heru.types import RuntimeEngineContinuation


class _StubManager:
    last_init: tuple[Path, Path] | None = None

    def __init__(self, workspace_root, *, execution_root=None):
        self.workspace_root = workspace_root
        self.execution_root = execution_root
        _StubManager.last_init = (Path(workspace_root), Path(execution_root))

    def run(self, task, **kwargs) -> SubagentResult:
        del task, kwargs
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


def test_heru_engine_adapter_updates_session_from_subagent_result_continuation(
    tmp_path, monkeypatch
) -> None:
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


def test_latest_verdict_after_rejects_empty_implementing_pass(tmp_path, monkeypatch) -> None:
    from litehive.state.records import create_task

    task = create_task(tmp_path, title="empty pass")
    append_thread_comment(
        tmp_path,
        task,
        TaskThreadComment(
            role="swe",
            step="implementing",
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
    append_thread_comment(
        tmp_path,
        task,
        TaskThreadComment(
            role="swe",
            step="implementing",
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
