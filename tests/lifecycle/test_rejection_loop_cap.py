import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.workspace import ensure_workspace
from litehive.config.workspace_files import config_path
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.orchestration import run_task as run_pipeline_task
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.state.records import create_task, get_task, save_task
from litehive.tasks.worktrees import resolve_recorded_worktree_path, task_worktree_branch

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
    ensure_workspace(root)
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
    task = create_task(tmp_path, title="Looping QA task")
    engine = _StageScriptEngine({"testing": ["reject", "reject", "reject"]})

    result = run_pipeline_task(tmp_path, task, engine_factory=lambda _: engine)
    refreshed = get_task(tmp_path, task.id)
    pipeline_state = SqlitePersistence(tmp_path).load(task.id)

    assert result.final_stage == "failed"
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.flag_reason == "rejection_loop_detected"
    assert pipeline_state.rejection_loop is not None
    assert pipeline_state.rejection_loop.rejection_stage == "testing"
    assert pipeline_state.rejection_loop.count == 3
    assert refreshed.runtime.git.worktree_path is not None

    worktree = resolve_recorded_worktree_path(tmp_path, refreshed.runtime.git.worktree_path)
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
    task = create_task(tmp_path, title="Reset QA loop after progress")
    engine = _StageScriptEngine(
        {
            "testing": ["reject", "reject", "pass", "reject", "pass"],
            "accepting": ["reject", "pass"],
        }
    )

    result = run_pipeline_task(tmp_path, task, engine_factory=lambda _: engine)
    refreshed = get_task(tmp_path, task.id)

    assert result.final_stage == "done"
    assert refreshed is not None
    assert refreshed.status == "done"
    assert engine.calls.count("testing") == 5


def test_task_rejection_loop_limit_overrides_workspace_default(tmp_path: Path) -> None:
    _init_workspace_git_repo(tmp_path, config=LitehiveConfig(default_rejection_loop_limit=5))
    task = create_task(tmp_path, title="Task-specific loop cap")
    task.retry_policy.rejection_loop_limit = 2
    save_task(tmp_path, task)
    engine = _StageScriptEngine({"testing": ["reject", "reject"]})

    result = run_pipeline_task(tmp_path, task, engine_factory=lambda _: engine)
    refreshed = get_task(tmp_path, task.id)
    pipeline_state = SqlitePersistence(tmp_path).load(task.id)

    assert result.final_stage == "failed"
    assert refreshed is not None
    assert refreshed.flag_reason == "rejection_loop_detected"
    assert pipeline_state.rejection_loop is not None
    assert pipeline_state.rejection_loop.count == 2
    assert engine.calls.count("testing") == 2
