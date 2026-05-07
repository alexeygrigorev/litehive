import subprocess
from pathlib import Path

import pytest

from litehive.config.workspace import create_workspace
from litehive.domain.reports import TaskActivityEntry
from litehive.lifecycle.nodes.agent import AgentVerdict, UnrecoverableError
from litehive.lifecycle.orchestration import run_task as run_pipeline_task
from litehive.state.records import create_task, get_task
from litehive.tasks.status import requeue_task_for_workspace
from litehive.workspace import Workspace

pytestmark = pytest.mark.integration


class _RepeatRecoveryEscalationEngine:
    def __init__(self, name: str, *, workspace: Path, follow_up_ids: list[str]) -> None:
        self.name = name
        self.workspace = workspace
        self.follow_up_ids = follow_up_ids

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session
        if prompt.role != "recovery":
            raise UnrecoverableError("repeatable infra crash")
        repeated = getattr(prompt, "repeated_recovery_fingerprint", None)
        if not repeated:
            return AgentVerdict(outcome="resume", metadata={"target_stage": "implementing"})

        follow_up = create_task(
            self.workspace,
            title=f"Recovery follow-up: {repeated['fingerprint']}",
            goal=f"Fix the repeated recovery failure for `{repeated['fingerprint']}`.",
            acceptance_criteria=[
                f"Root cause for `{repeated['fingerprint']}` is identified",
                "Recovery no longer needs to re-route this failure path",
            ],
        )
        self.follow_up_ids.append(follow_up.id)
        task = get_task(self.workspace, state.task_id)
        assert task is not None
        message = f"Repeated recovery fingerprint `{repeated['fingerprint']}`. Filed follow-up task {follow_up.id}."
        Workspace.from_path(self.workspace).task_activity(task).append(
            TaskActivityEntry(
                role="recovery",
                stage="recovering",
                verdict="reject",
                message=message,
                follow_up_task_id=follow_up.id,
            )
        )
        return AgentVerdict(outcome="reject", reason=message)


def _init_workspace_git_repo(root: Path) -> None:
    create_workspace(root)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def _run_with_repeat_engine(tmp_path: Path, task, follow_up_ids: list[str]):
    return run_pipeline_task(
        tmp_path,
        task,
        engine_factory=lambda name: _RepeatRecoveryEscalationEngine(
            name, workspace=tmp_path, follow_up_ids=follow_up_ids
        ),
    )


def test_recovery_repeat_fingerprint_escalates_after_requeue(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    task = create_task(tmp_path, title="Repeated recovery fingerprint", pipeline_mode="single")
    follow_up_ids: list[str] = []

    first = _run_with_repeat_engine(tmp_path, task, follow_up_ids)
    first_refreshed = get_task(tmp_path, task.id)
    assert first.final_stage == "failed"
    assert first_refreshed is not None
    assert first_refreshed.flag_reason == "crash_budget_exhausted"
    assert len(first_refreshed.runtime.pipeline.recovery_history) == 2
    assert follow_up_ids == []

    second = _run_with_repeat_engine(
        tmp_path,
        requeue_task_for_workspace(Workspace.from_path(tmp_path), task.id),
        follow_up_ids,
    )
    second_refreshed = get_task(tmp_path, task.id)
    assert second.final_stage == "failed"
    assert second_refreshed is not None
    assert second_refreshed.flag_reason == "recovery_failed"
    assert len(second_refreshed.runtime.pipeline.recovery_history) == 3
    assert len(follow_up_ids) == 1
    assert second_refreshed.runtime.pipeline.last_outcome.follow_up_task_id == follow_up_ids[0]

    follow_up = get_task(tmp_path, follow_up_ids[0])
    assert follow_up is not None
    assert "repeatable infra crash" in follow_up.goal
