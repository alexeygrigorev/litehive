from litehive.agents.models import SubagentResult
from litehive.models import SubagentRef
from litehive.pipeline.heru_factory import HeruEngineAdapter
from litehive.pipeline.nodes.agent import AgentVerdict
from litehive.pipeline.persistence import TaskState
from litehive.pipeline.sessions import Session
from litehive.pipeline.types import PipelineMode
from heru.types import RuntimeEngineContinuation


class _StubManager:
    def __init__(self, workspace_root):
        self.workspace_root = workspace_root

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
    from litehive.tasks.crud import create_task

    task = create_task(tmp_path, title="resume", goal="keep continuation")
    session = Session()
    state = TaskState(task_id=task.id, stage="implementing", pipeline_mode=PipelineMode.FULL)
    adapter = HeruEngineAdapter("codex", tmp_path)

    monkeypatch.setattr("litehive.pipeline.heru_factory.SubagentManager", _StubManager)
    monkeypatch.setattr(
        "litehive.pipeline.heru_factory._latest_verdict_after",
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
