from pathlib import Path

from litehive.agents.session_store import (
    load_subagent_report,
    load_subagent_session,
    save_subagent_artifacts,
)
from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.journal import SqliteJournal
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.system import StubCommitNode
from litehive.lifecycle.orchestration import run_task
from litehive.domain.runtime import RuntimeSubagentState
from litehive.recovery.workspace_repair import (
    prepare_interrupted_task,
    recover_stale_runner_state,
)
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.queue import dequeue_next_task
from litehive.tasks.status import resume_task


class _PassEngine:
    def __init__(self, name: str) -> None:
        self.name = name

    def run_turn(self, session, prompt, state) -> AgentVerdict:  # type: ignore[no-untyped-def]
        return AgentVerdict(outcome="pass")


def test_recover_stale_runner_state_requeues_running_stage(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Crash recovery")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.stage = "implementing"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "implementing"
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == "implementing"
    assert refreshed.runtime.current_stage.status == "idle"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.stage == "implementing"
    assert refreshed.runtime.interruption.resume_stage == "implementing"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task.id


def test_recover_stale_runner_state_requeues_commit_stage_as_queued(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Commit recovery")
    task.status = "in_progress"
    task.pipeline_status = "commit_to_git"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.stage = "commit_to_git"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "commit_to_git"
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == "commit_to_git"
    assert refreshed.runtime.current_stage.status == "idle"
    assert refreshed.runtime.interruption is not None
    assert refreshed.runtime.interruption.resume_stage == "commit_to_git"


def test_recover_stale_runner_state_requeues_running_task_without_active_task_id(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Running without active task id")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.stage = "implementing"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = None
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.runtime.execution_status == "idle"
    assert refreshed.runtime.current_stage.stage == "implementing"
    assert refreshed.runtime.current_stage.status == "idle"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.queue[0] == task.id


def test_resume_task_allows_stranded_in_progress_interrupted_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume stranded task")
    task.status = "in_progress"
    task.pipeline_status = "grooming"
    task.runtime.execution_status = "interrupted"
    task.runtime.current_stage.stage = "grooming"
    task.runtime.current_stage.status = "interrupted"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [item for item in state.queue if item != task.id]
    save_state(tmp_path, state)

    resumed = resume_task(tmp_path, task.id, front=True)

    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.current_stage.stage == "grooming"
    assert resumed.runtime.current_stage.status == "idle"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task.id


def test_resume_task_allows_stranded_in_progress_idle_task(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Resume idle stranded task")
    task.status = "in_progress"
    task.pipeline_status = "grooming"
    task.runtime.execution_status = "idle"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = None
    state.queue = [item for item in state.queue if item != task.id]
    save_state(tmp_path, state)

    resumed = resume_task(tmp_path, task.id, front=True)

    assert resumed.status == "queued"
    assert resumed.pipeline_status == "grooming"
    assert resumed.runtime.execution_status == "idle"
    assert resumed.runtime.current_stage.stage == "grooming"
    assert resumed.runtime.current_stage.status == "idle"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None
    assert refreshed_state.queue[0] == task.id


def test_recover_stale_runner_state_clears_non_running_active_task_id(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Flagged but not running")
    task.status = "flagged"
    task.pipeline_status = "flagged"
    task.runtime.execution_status = "idle"
    save_task(tmp_path, task)

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.runtime.execution_status == "idle"

    refreshed_state = load_state(tmp_path)
    assert refreshed_state.active_task_id is None


def test_prepare_interrupted_task_writes_resume_bookkeeping(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Interrupted run")
    subagent_base = tmp_path / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "subagents" / "SA-1234-swe"
    subagent_base.mkdir(parents=True)
    save_subagent_artifacts(
        tmp_path,
        task.id,
        "SA-1234",
        session={"status": "running"},
        report={"summary": "finished half the change"},
    )

    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.stage = "implementing"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    task.runtime.active_subagent = RuntimeSubagentState(
        id="SA-1234",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-1234-swe",
        sandboxed=False,
        sandbox_summary="host",
        started_at="2026-04-12T10:00:00Z",
        updated_at="2026-04-12T10:05:00Z",
    )

    prepare_interrupted_task(
        tmp_path,
        task,
        stage="implementing",
        summary="Interrupted run recovered. Resume from `implementing`.",
        reason="received ctrl-c",
    )

    assert task.runtime.interruption is not None
    assert task.runtime.interruption.reason == "received ctrl-c"
    assert task.runtime.interruption.resume_stage == "implementing"
    assert task.runtime.active_subagent is None
    assert task.runtime.last_subagent is not None
    assert task.runtime.last_subagent.status == "interrupted"

    session = load_subagent_session(tmp_path, task.id, "SA-1234")
    report = load_subagent_report(tmp_path, task.id, "SA-1234")
    assert session["status"] == "interrupted"
    assert session["resume_stage"] == "implementing"
    assert session["interruption_reason"] == "received ctrl-c"
    assert report["status"] == "interrupted"
    assert report["resume_stage"] == "implementing"
    assert report["interruption_reason"] == "received ctrl-c"


def test_restarted_execution_enters_saved_resumable_stage(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    task = create_task(
        tmp_path,
        title="Restart saved stage",
        acceptance_criteria=["resume in testing without replaying earlier stages"],
    )
    task.status = "parked"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "paused"
    task.runtime.current_stage.stage = "testing"
    task.runtime.current_stage.status = "paused"
    save_task(tmp_path, task)

    resumed = resume_task(tmp_path, task.id, front=True)
    assert resumed.runtime.current_stage.stage == "testing"
    assert resumed.runtime.current_stage.status == "idle"

    monkeypatch.setattr(
        "litehive.lifecycle.orchestration._build_commit_node",
        lambda root: StubCommitNode(),
    )

    queued = dequeue_next_task(tmp_path)
    assert queued is not None

    result = run_task(
        tmp_path,
        queued,
        engine_factory=lambda name: _PassEngine(name),
    )
    transitions = SqliteJournal(tmp_path).load_transitions(task.id)
    routed_stages = [row["to_stage"] for row in transitions]

    assert result.final_stage == "done"
    assert routed_stages[:2] == ["worktree_sync", "before_testing"]
    assert "before_grooming" not in routed_stages
    assert "before_implementing" not in routed_stages
