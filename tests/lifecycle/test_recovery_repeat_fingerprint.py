import subprocess
from pathlib import Path

import pytest

from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import TaskActivityEntry
from litehive.lifecycle.nodes.agent import AgentVerdict, UnrecoverableError
from litehive.lifecycle.orchestration import run_task as run_pipeline_task
from litehive.state.records import create_task, get_task
from litehive.tasks.activity import append_task_activity
from litehive.tasks.status import requeue_task

pytestmark = pytest.mark.integration


class _RepeatRecoveryEscalationEngine:
    def __init__(self, name: str, *, workspace: Path, follow_up_ids: list[str]) -> None:
        self.name = name
        self.workspace = workspace
        self.follow_up_ids = follow_up_ids

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session
        if prompt["role"] != "recovery":
            raise UnrecoverableError("repeatable infra crash")

        repeated = prompt.get("repeated_recovery_fingerprint")
        if not repeated:
            return AgentVerdict(outcome="resume", metadata={"target_stage": "implementing"})

        follow_up = create_task(
            self.workspace,
            title=f"Recovery follow-up: {repeated['fingerprint']}",
            task_type="bugfix",
            goal=(
                "Fix the repeated recovery failure so the task no longer re-enters recovery for "
                f"`{repeated['fingerprint']}`."
            ),
            acceptance_criteria=[
                f"Root cause for `{repeated['fingerprint']}` is identified",
                "Recovery no longer needs to re-route this failure path",
            ],
        )
        self.follow_up_ids.append(follow_up.id)
        task = get_task(self.workspace, state.task_id)
        assert task is not None
        append_task_activity(
            self.workspace,
            task,
            TaskActivityEntry(
                role="recovery",
                stage="recovering",
                verdict="reject",
                message=(
                    f"Repeated recovery fingerprint `{repeated['fingerprint']}`. "
                    f"Filed follow-up task {follow_up.id} instead of re-routing."
                ),
                follow_up_task_id=follow_up.id,
            ),
        )
        return AgentVerdict(
            outcome="reject",
            reason=(
                f"Repeated recovery fingerprint `{repeated['fingerprint']}`. "
                f"Filed follow-up task {follow_up.id} instead of re-routing."
            ),
        )


def _init_workspace_git_repo(root: Path) -> None:
    ensure_workspace(root)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def test_recovery_repeat_fingerprint_escalates_after_requeue(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    task = create_task(tmp_path, title="Repeated recovery fingerprint", pipeline_mode="single")
    follow_up_ids: list[str] = []

    first = run_pipeline_task(
        tmp_path,
        task,
        engine_factory=lambda engine_name: _RepeatRecoveryEscalationEngine(
            engine_name,
            workspace=tmp_path,
            follow_up_ids=follow_up_ids,
        ),
    )
    first_refreshed = get_task(tmp_path, task.id)

    assert first.final_stage == "failed"
    assert first_refreshed is not None
    assert first_refreshed.status == "flagged"
    assert first_refreshed.flag_reason == "crash_budget_exhausted"
    assert len(first_refreshed.runtime.recovery_history) == 2
    assert follow_up_ids == []

    requeued = requeue_task(tmp_path, task.id)
    second = run_pipeline_task(
        tmp_path,
        requeued,
        engine_factory=lambda engine_name: _RepeatRecoveryEscalationEngine(
            engine_name,
            workspace=tmp_path,
            follow_up_ids=follow_up_ids,
        ),
    )
    second_refreshed = get_task(tmp_path, task.id)

    assert second.final_stage == "failed"
    assert second_refreshed is not None
    assert second_refreshed.status == "flagged"
    assert second_refreshed.flag_reason == "recovery_failed"
    assert len(second_refreshed.runtime.recovery_history) == 3
    assert len(follow_up_ids) == 1
    assert second_refreshed.runtime.last_outcome.follow_up_task_id == follow_up_ids[0]

    follow_up = get_task(tmp_path, follow_up_ids[0])
    assert follow_up is not None
    assert follow_up.task_type == "bugfix"
    assert "repeatable infra crash" in follow_up.goal
