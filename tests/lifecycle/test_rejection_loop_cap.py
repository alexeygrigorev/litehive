import argparse
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import create_workspace
from litehive.config.workspace_files import config_path
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.orchestration import run_task_for_workspace
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.workspace import Workspace
from litehive.state.records import create_task_for_workspace, get_task_for_workspace, save_task_for_workspace
from litehive.tasks.status import requeue_task_for_workspace
from litehive.worktree.paths import resolve_recorded_worktree_path_for_workspace, task_worktree_branch

from tests.support.helpers import _cmd_status

pytestmark = pytest.mark.integration


class _StageScriptEngine:
    def __init__(self, stage_outcomes: dict[str, list[str]]) -> None:
        self.name = "stub"
        self.stage_outcomes = {stage: list(outcomes) for stage, outcomes in stage_outcomes.items()}
        self.calls: list[str] = []

    def run_turn(self, session, prompt, state) -> AgentVerdict:
        del prompt
        self.calls.append(state.stage)
        outcomes = self.stage_outcomes.setdefault(state.stage, ["pass"])
        outcome = outcomes.pop(0) if outcomes else "pass"
        session.engine_session_id = f"{state.stage}-{len(self.calls)}"
        if outcome == "reject":
            return AgentVerdict(outcome="reject", reason=f"{state.stage} asked for another implementation pass")
        return AgentVerdict(outcome="pass")


def _init_workspace_git_repo(root: Path, *, config: LitehiveConfig | None = None) -> None:
    create_workspace(root)
    if config is not None:
        config_path(root).write_text(yaml.safe_dump(asdict(config), sort_keys=False), encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def test_rejection_loop_flags_task_preserves_worktree_and_branch(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Looping QA task")
    engine = _StageScriptEngine({"testing": ["reject", "reject", "reject"]})

    result = run_task_for_workspace(workspace, workspace.load_config(), task, engine_factory=lambda _: engine)
    refreshed = get_task_for_workspace(workspace, task.id)
    pipeline_state = SqlitePersistence(workspace).load(task.id)

    assert result.final_stage == "failed"
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.flag_reason == "rejection_loop_detected"
    assert pipeline_state.rejection_loop is not None
    assert pipeline_state.rejection_loop.rejection_stage == "testing"
    assert pipeline_state.rejection_loop.count == 3
    assert refreshed.runtime.pipeline.git.worktree_path is not None

    worktree = resolve_recorded_worktree_path_for_workspace(
        workspace,
        refreshed.runtime.pipeline.git.worktree_path,
    )
    assert worktree is not None
    assert worktree.exists()

    branch = task_worktree_branch(refreshed)
    branch_list = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch in branch_list.stdout


def test_rejection_loop_counter_resets_after_testing_pass(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Reset QA loop after progress")
    engine = _StageScriptEngine(
        {
            "testing": ["reject", "reject", "pass", "reject", "pass"],
            "accepting": ["reject", "pass"],
        }
    )

    result = run_task_for_workspace(workspace, workspace.load_config(), task, engine_factory=lambda _: engine)
    refreshed = get_task_for_workspace(workspace, task.id)

    assert result.final_stage == "done"
    assert refreshed is not None
    assert refreshed.status == "done"
    assert engine.calls.count("testing") == 5


def test_task_rejection_loop_limit_overrides_workspace_default(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path, config=LitehiveConfig(default_rejection_loop_limit=5))
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Task-specific loop cap")
    task.retry_policy.rejection_loop_limit = 2
    save_task_for_workspace(workspace, task)
    engine = _StageScriptEngine({"testing": ["reject", "reject"]})

    result = run_task_for_workspace(workspace, workspace.load_config(), task, engine_factory=lambda _: engine)
    refreshed = get_task_for_workspace(workspace, task.id)
    pipeline_state = SqlitePersistence(workspace).load(task.id)

    assert result.final_stage == "failed"
    assert refreshed is not None
    assert refreshed.flag_reason == "rejection_loop_detected"
    assert pipeline_state.rejection_loop is not None
    assert pipeline_state.rejection_loop.count == 2
    assert engine.calls.count("testing") == 2


def test_repeated_stage_retry_exhaustion_survives_requeue_and_blocks_blind_requeue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init_workspace_git_repo(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task_for_workspace(workspace, title="Repeated implementing reject", pipeline_mode="single")
    first_engine = _StageScriptEngine({"implementing": ["reject", "reject", "reject", "reject"]})

    first = run_task_for_workspace(workspace, workspace.load_config(), task, engine_factory=lambda _: first_engine)
    first_refreshed = get_task_for_workspace(workspace, task.id)
    first_state = SqlitePersistence(workspace).load(task.id)

    assert first.final_stage == "failed"
    assert first_refreshed is not None
    assert first_refreshed.status == "flagged"
    assert first_refreshed.flag_reason == "semantic_reject"
    assert first_state.failed_run_history
    first_record = next(iter(first_state.failed_run_history.values()))
    assert first_record.stage == "implementing"
    assert first_record.count == 1
    assert first_refreshed.runtime.pipeline.failed_run_history[first_record.key].count == 1

    requeued = requeue_task_for_workspace(workspace, task.id)
    second_engine = _StageScriptEngine({"implementing": ["reject", "reject", "reject", "reject"]})
    second = run_task_for_workspace(workspace, workspace.load_config(), requeued, engine_factory=lambda _: second_engine)
    second_refreshed = get_task_for_workspace(workspace, task.id)
    second_state = SqlitePersistence(workspace).load(task.id)

    assert second.final_stage == "failed"
    assert second_refreshed is not None
    assert second_refreshed.status == "flagged"
    second_record = next(iter(second_state.failed_run_history.values()))
    assert second_record.stage == "implementing"
    assert second_record.count == 2
    assert second_refreshed.runtime.pipeline.failed_run_history[second_record.key].count == 2

    with pytest.raises(ValueError, match="repeatedly exhausted the same stage retry budget"):
        requeue_task_for_workspace(workspace, task.id)

    forced = requeue_task_for_workspace(workspace, task.id, force=True)
    forced_record = next(iter(forced.runtime.pipeline.failed_run_history.values()))
    assert forced.status == "queued"
    assert forced_record.count == 2
    assert forced_record.operator_override_count == 2

    exit_code = _cmd_status(argparse.Namespace(workspace=tmp_path, full=True))
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "failed_run_history:" in output
    assert "stage=implementing" in output
    assert "count=2" in output
    assert "operator_override_count=2" in output
