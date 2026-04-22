import subprocess
from pathlib import Path

from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.orchestration import run_task as run_pipeline_task
from litehive.lifecycle.persistence import LastReport, TaskState
from litehive.lifecycle.types import PipelineMode
from litehive.config.workspace import ensure_workspace
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.roles.base import PromptContext
from litehive.roles.recovery import RecoveryAgent
from litehive.state.records import create_task, get_task, save_task


class _NullSelector:
    def select(self, state, node_name, excluded):
        del state, node_name, excluded
        return None


class _NullSessions:
    def get_or_create(self, task_id, node_name, engine_name):
        del task_id, node_name, engine_name
        return None

    def persist(self, task_id, node_name, engine_name, session):
        del task_id, node_name, engine_name, session


class _UnrelatedFailureEngine:
    def __init__(self) -> None:
        self.name = "stub"
        self.calls: list[str] = []

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del session, prompt
        self.calls.append(state.stage)
        if state.stage == "grooming":
            return AgentVerdict(outcome="pass")
        if state.stage == "implementing":
            return AgentVerdict(
                outcome="pass",
                metadata={"files_changed": ["litehive/tasks/archive.py"]},
            )
        if state.stage == "testing":
            return AgentVerdict(outcome="pass")
        if state.stage == "accepting":
            return AgentVerdict(
                outcome="reject",
                reason=(
                    "Acceptance failed:\n"
                    "- uv run pytest -q "
                    "tests/lifecycle/test_recovery_role.py::"
                    "test_recovery_agent_preserves_target_stage_for_resume_and_advance"
                ),
            )
        raise AssertionError(f"unexpected engine call for stage {state.stage}")


def _init_workspace_git_repo(root: Path) -> None:
    ensure_workspace(root)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def _recovery_state(task_id: str, *, changed_files: list[str], message: str) -> TaskState:
    return TaskState(
        task_id=task_id,
        stage="recovering",
        pipeline_mode=PipelineMode.FULL,
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="accepting",
            trigger_event_kind=TriggerEventKind.REJECT,
            failure_fingerprint=FailureFingerprint(
                fingerprint=f"agent:{message}",
                classification="agent_reject",
            ),
            source="agent",
            message=message,
        ),
        last_report=LastReport(
            files_changed=len(changed_files),
            changed_files=changed_files,
        ),
    )


def test_recovery_agent_preserves_target_stage_for_resume_and_advance() -> None:
    agent = object.__new__(RecoveryAgent)

    resume = agent._verdict_to_event(AgentVerdict(outcome="resume", metadata={"target_stage": "testing"}))
    advance = agent._verdict_to_event(AgentVerdict(outcome="advance", metadata={"target_stage": "accepting"}))

    assert resume.resume == "testing"
    assert advance.resume == "accepting"


def test_recovery_agent_fails_resume_without_target_stage() -> None:
    agent = object.__new__(RecoveryAgent)

    event = agent._verdict_to_event(AgentVerdict(outcome="resume", metadata={}))

    assert event.reason == "recovery resume verdict missing target_stage"


def test_recovery_agent_attributes_changed_surface_failure() -> None:
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    prompt = agent.build_prompt(
        _recovery_state(
            "T-0001",
            changed_files=["litehive/roles/recovery.py"],
            message=(
                "uv run pytest -q tests/lifecycle/test_recovery_role.py::"
                "test_recovery_agent_preserves_target_stage_for_resume_and_advance"
            ),
        )
    )

    assert prompt["test_failure_attribution"]["classification"] == "changed_surface"


def test_recovery_agent_attributes_unrelated_breakage() -> None:
    agent = RecoveryAgent(_NullSelector(), _NullSessions(), prompt_context=PromptContext())
    prompt = agent.build_prompt(
        _recovery_state(
            "T-0001",
            changed_files=["litehive/tasks/archive.py"],
            message=(
                "uv run pytest -q tests/lifecycle/test_recovery_role.py::"
                "test_recovery_agent_preserves_target_stage_for_resume_and_advance"
            ),
        )
    )

    assert prompt["test_failure_attribution"]["classification"] == "unrelated_breakage"


def test_recovery_agent_files_follow_up_for_unrelated_test_failure(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    task = create_task(tmp_path, title="Primary task blocked by unrelated failure")
    task.retry_policy.rejection_loop_limit = 10
    save_task(tmp_path, task)
    engine = _UnrelatedFailureEngine()

    result = run_pipeline_task(tmp_path, task, engine_factory=lambda _: engine)
    refreshed = get_task(tmp_path, task.id)
    follow_up = get_task(tmp_path, "T-0002")

    assert result.final_stage == "failed"
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.flag_reason == "blocked_on_follow_up:T-0002"
    assert refreshed.runtime.last_outcome.kind == "blocked"
    assert refreshed.runtime.last_outcome.reason_code == "blocked_on_follow_up"
    assert refreshed.runtime.last_outcome.follow_up_task_id == "T-0002"

    assert follow_up is not None
    assert follow_up.task_type == "bugfix"
    assert follow_up.created_from is not None
    assert follow_up.created_from.task_id == task.id
    assert follow_up.created_from.stage == "accepting"
    assert follow_up.created_from.blocking is True
    assert "test_recovery_role.py" in follow_up.title
    assert "test_recovery_agent_preserves_target_stage_for_resume_and_advance" in follow_up.goal

    assert "recovering" not in engine.calls
