import subprocess
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.lifecycle.journal import SqliteJournal
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.nodes.system import StubCommitNode
from litehive.lifecycle.orchestration import run_task
from litehive.lifecycle.persistence import RejectionLoop, SqlitePersistence, TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.recovery.workspace_repair import recover_stale_runner_state
from litehive.state.persist import load_state, save_state
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.completed_task_recovery import recover_completed_task
from litehive.tasks.queue import dequeue_next_task
from litehive.tasks.status import requeue_task, resume_task


class _PassEngine:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def run_turn(self, session, prompt, state) -> AgentVerdict:  # type: ignore[no-untyped-def]
        del prompt
        self.calls.append(state.stage)
        session.engine_session_id = f"{state.stage}-{len(self.calls)}"
        return AgentVerdict(outcome="pass")


def _init_workspace_git_repo(root: Path) -> None:
    ensure_workspace(root)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def _seed_terminal_pipeline_state(root: Path, task_id: str, *, entry_stage: str, stage: str = "failed") -> None:
    SqlitePersistence(root).save(
        TaskState(
            task_id=task_id,
            stage=stage,
            pipeline_mode=PipelineMode.FULL,
            entry_stage=entry_stage,
            rejection_loop=RejectionLoop(
                rejection_stage="testing",
                retry_target_stage="implementing",
                count=2,
            ),
        )
    )


def test_run_task_recovers_retry_target_stage_load_failure_for_resumed_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_workspace_git_repo(tmp_path)
    task = create_task(tmp_path, title="Resume queued task after retry-target crash")
    task.status = "parked"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "paused"
    task.runtime.current_stage.stage = "testing"
    task.runtime.current_stage.status = "paused"
    save_task(tmp_path, task)

    resumed = resume_task(tmp_path, task.id, front=True)
    _seed_terminal_pipeline_state(tmp_path, resumed.id, entry_stage="testing")

    queued = dequeue_next_task(tmp_path)
    assert queued is not None

    original_load = SqlitePersistence.load
    seen_retry_target_stage_error = {"value": False}

    def flaky_load(self, task_id: str):  # type: ignore[no-untyped-def]
        if task_id == resumed.id and not seen_retry_target_stage_error["value"]:
            seen_retry_target_stage_error["value"] = True
            raise NameError("name 'retry_target_stage' is not defined")
        return original_load(self, task_id)

    monkeypatch.setattr("litehive.lifecycle.orchestration.SqlitePersistence.load", flaky_load)
    monkeypatch.setattr(
        "litehive.lifecycle.orchestration._build_commit_node",
        lambda root: StubCommitNode(),
    )

    engine = _PassEngine("stub")
    result = run_task(tmp_path, queued, engine_factory=lambda _: engine)
    routed_stages = [row["to_stage"] for row in SqliteJournal(tmp_path).load_transitions(task.id)]

    assert seen_retry_target_stage_error["value"] is True
    assert result.final_stage == "done"
    assert engine.calls == ["testing", "accepting"]
    assert routed_stages[:2] == ["worktree_sync", "before_testing"]
    assert "before_grooming" not in routed_stages
    assert "before_implementing" not in routed_stages


def test_run_task_recovers_retry_target_stage_load_failure_for_requeued_deferred_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_workspace_git_repo(tmp_path)
    task = create_task(
        tmp_path,
        title="Requeue deferred task after retry-target crash",
        acceptance_criteria=["resume the retry target stage"],
    )
    task.status = "deferred"
    task.flag_count = 3
    task.flag_reason = "flagged 3 times - needs human review"
    task.pipeline_status = "implementing"
    task.runtime.last_stage.stage = "accepting"
    task.runtime.last_stage.verdict = "pass"
    task.runtime.last_stage.summary = "Task already produced a valid pass report."
    save_task(tmp_path, task)

    requeued = requeue_task(tmp_path, task.id, force=True, front=True)
    _seed_terminal_pipeline_state(tmp_path, requeued.id, entry_stage="implementing")

    queued = dequeue_next_task(tmp_path)
    assert queued is not None

    original_load = SqlitePersistence.load
    seen_retry_target_stage_error = {"value": False}

    def flaky_load(self, task_id: str):  # type: ignore[no-untyped-def]
        if task_id == requeued.id and not seen_retry_target_stage_error["value"]:
            seen_retry_target_stage_error["value"] = True
            raise NameError("name 'retry_target_stage' is not defined")
        return original_load(self, task_id)

    monkeypatch.setattr("litehive.lifecycle.orchestration.SqlitePersistence.load", flaky_load)
    monkeypatch.setattr(
        "litehive.lifecycle.orchestration._build_commit_node",
        lambda root: StubCommitNode(),
    )

    engine = _PassEngine("stub")
    result = run_task(tmp_path, queued, engine_factory=lambda _: engine)
    routed_stages = [row["to_stage"] for row in SqliteJournal(tmp_path).load_transitions(task.id)]
    refreshed = get_task(tmp_path, task.id)

    assert refreshed is not None
    assert seen_retry_target_stage_error["value"] is True
    assert result.final_stage == "done"
    assert refreshed.status == "done"
    assert refreshed.flag_count == 3
    assert engine.calls == ["implementing", "testing", "accepting"]
    assert routed_stages[:2] == ["worktree_sync", "before_implementing"]
    assert "failed" not in routed_stages


def test_run_task_restarts_recovered_stale_runner_task_from_ready(tmp_path: Path, monkeypatch) -> None:
    _init_workspace_git_repo(tmp_path)
    task = create_task(tmp_path, title="Recover stale runner task")
    task.status = "in_progress"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "running"
    task.runtime.run_started_at = "2026-04-12T10:00:00Z"
    task.runtime.current_stage.stage = "implementing"
    task.runtime.current_stage.status = "running"
    task.runtime.current_stage.started_at = "2026-04-12T10:00:00Z"
    save_task(tmp_path, task)
    _seed_terminal_pipeline_state(tmp_path, task.id, entry_stage="implementing")

    state = load_state(tmp_path)
    state.active_task_id = task.id
    save_state(tmp_path, state)

    assert recover_stale_runner_state(tmp_path) is True

    queued = dequeue_next_task(tmp_path)
    assert queued is not None

    monkeypatch.setattr(
        "litehive.lifecycle.orchestration._build_commit_node",
        lambda root: StubCommitNode(),
    )

    engine = _PassEngine("stub")
    result = run_task(tmp_path, queued, engine_factory=lambda _: engine)
    routed_stages = [row["to_stage"] for row in SqliteJournal(tmp_path).load_transitions(task.id)]

    assert result.final_stage == "done"
    assert engine.calls == ["implementing", "testing", "accepting"]
    assert routed_stages[:2] == ["worktree_sync", "before_implementing"]


def test_run_task_restarts_recovered_completed_task_from_ready(tmp_path: Path, monkeypatch) -> None:
    _init_workspace_git_repo(tmp_path)
    task = create_task(tmp_path, title="Recover completed task")
    task.status = "done"
    task.pipeline_status = "done"
    task.runtime.last_stage.stage = "accepting"
    task.runtime.last_stage.verdict = "pass"
    task.runtime.last_stage.summary = "Task already passed once."
    save_task(tmp_path, task)
    _seed_terminal_pipeline_state(tmp_path, task.id, entry_stage="implementing", stage="done")

    recovered = recover_completed_task(tmp_path, task.id)
    queued = dequeue_next_task(tmp_path)
    assert queued is not None
    assert queued.id == recovered.id

    monkeypatch.setattr(
        "litehive.lifecycle.orchestration._build_commit_node",
        lambda root: StubCommitNode(),
    )

    engine = _PassEngine("stub")
    result = run_task(tmp_path, queued, engine_factory=lambda _: engine)
    routed_stages = [row["to_stage"] for row in SqliteJournal(tmp_path).load_transitions(task.id)]

    assert result.final_stage == "done"
    assert engine.calls == ["implementing", "testing", "accepting"]
    assert routed_stages[:2] == ["worktree_sync", "before_implementing"]
