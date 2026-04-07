"""Tests for the worktree rebase/merge flow.

Scenario matrix:
1. Rebase succeeds — no merge agent needed, task proceeds
2. Rebase fails, git merge succeeds — task proceeds without agent
3. Rebase fails, merge has conflict, agent resolves — task completes
4. Rebase fails, merge has conflict, agent fails — task gets merge_failed status
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from litehive.config import LitehiveConfig, ensure_workspace
from litehive.git_ops import current_head
from litehive.models import SubagentRef, UnmergedWorktree
from litehive.runtime import _commit_to_git_report
from litehive.subagents import SubagentResult
from litehive.tasks import create_task, load_state, save_state, save_task


def _init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    (path / "app.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)
    return current_head(path)


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout.strip()


def _setup_worktree(tmp_path: Path, task, *, feature_content="def feature(): return True\n"):
    """Create a worktree with a feature.py file and return its path."""
    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "feature.py").write_text(feature_content)
    _run(["git", "add", "feature.py"], worktree_path)
    _run(["git", "commit", "-m", "add feature"], worktree_path)
    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)
    return worktree_path


def _create_conflict(tmp_path: Path, content="def feature(): return 'main'\n"):
    """Create a conflicting feature.py on main."""
    (tmp_path / "feature.py").write_text(content)
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change on main"], tmp_path)


class _ResolvingSubagents:
    """Fake subagent that resolves merge conflicts."""

    def __init__(self, root: Path, worktree_path: Path):
        self.execution_root = worktree_path
        self._root = root
        self.calls = []

    def run(self, task, *, role, engine_name, prompt, model=None):
        self.calls.append({"role": role, "engine": engine_name})
        # Resolve the conflict by picking worktree version
        conflict_file = self._root / "feature.py"
        conflict_file.write_text("def feature(): return 'worktree'\n")
        _run(["git", "add", "feature.py"], self._root)
        _run(["git", "commit", "--no-edit"], self._root)
        return SubagentResult(
            ref=SubagentRef(id="SA-merge", role=role, engine=engine_name,
                            status="completed", path="subagents/SA-merge"),
            execution=None, transcript="Resolved conflict", exit_code=0,
        )


class _FailingSubagents:
    """Fake subagent that fails to resolve merge conflicts."""

    def __init__(self, worktree_path: Path):
        self.execution_root = worktree_path
        self.calls = []

    def run(self, task, *, role, engine_name, prompt, model=None):
        self.calls.append({"role": role, "engine": engine_name})
        return SubagentResult(
            ref=SubagentRef(id="SA-merge", role=role, engine=engine_name,
                            status="completed", path="subagents/SA-merge"),
            execution=None, transcript="Could not resolve", exit_code=1,
        )


# ── Scenario 1: Rebase succeeds (no conflict) ───────────────────────────────

def test_rebase_succeeds_no_agent_needed(tmp_path: Path) -> None:
    """When worktree merges cleanly into main, no agent is launched."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Clean merge")

    worktree_path = _setup_worktree(tmp_path, task)
    # No conflicting changes on main — merge should succeed cleanly.

    subagents = _FailingSubagents(worktree_path)
    report = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=subagents, config=LitehiveConfig(),
    )

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert len(subagents.calls) == 0, "Agent should not be launched for clean merge"
    assert not worktree_path.exists(), "Worktree should be cleaned up"


# ── Scenario 2: Rebase fails, git merge succeeds ─────────────────────────────

def test_merge_succeeds_after_rebase_fail(tmp_path: Path) -> None:
    """When rebase fails but git merge succeeds, task proceeds without agent."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Merge without conflict")

    worktree_path = _setup_worktree(tmp_path, task, feature_content="def new_feature(): pass\n")

    # Add a non-conflicting change on main (different file)
    (tmp_path / "other.py").write_text("other = True\n")
    _run(["git", "add", "other.py"], tmp_path)
    _run(["git", "commit", "-m", "add other file on main"], tmp_path)

    subagents = _FailingSubagents(worktree_path)
    report = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=subagents, config=LitehiveConfig(),
    )

    assert report.verdict == "pass"
    assert task.status == "done"
    assert len(subagents.calls) == 0, "Agent should not be launched when merge succeeds"
    # Both files should exist on main
    assert (tmp_path / "feature.py").exists()
    assert (tmp_path / "other.py").exists()


# ── Scenario 3: Merge conflict, agent resolves ───────────────────────────────

def test_merge_conflict_agent_resolves(tmp_path: Path) -> None:
    """Agent resolves the merge conflict, task completes successfully."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent resolves")

    worktree_path = _setup_worktree(tmp_path, task, feature_content="def feature(): return 'worktree'\n")
    _create_conflict(tmp_path)

    subagents = _ResolvingSubagents(tmp_path, worktree_path)
    report = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=subagents, config=LitehiveConfig(),
    )

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.git.merge_agent_attempts == 1
    assert len(subagents.calls) == 1
    assert subagents.calls[0]["role"] == "merge-resolver"
    assert not worktree_path.exists(), "Worktree should be cleaned up after resolved merge"
    assert (tmp_path / "feature.py").read_text() == "def feature(): return 'worktree'\n"


# ── Scenario 4: Merge conflict, agent fails ──────────────────────────────────

def test_merge_conflict_agent_fails(tmp_path: Path) -> None:
    """Agent fails to resolve conflict. Task should fail with merge_failed classification."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent fails")

    worktree_path = _setup_worktree(tmp_path, task, feature_content="def feature(): return 'worktree'\n")
    _create_conflict(tmp_path)

    subagents = _FailingSubagents(worktree_path)
    report = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=subagents, config=LitehiveConfig(),
    )

    assert report.verdict == "fail"
    assert report.failure_classification == "merge_conflict"
    assert task.status != "done"
    assert task.git.merge_agent_attempts == 1
    assert len(subagents.calls) == 1
    assert worktree_path.exists(), "Worktree must survive failed merge"
    assert (worktree_path / "feature.py").read_text() == "def feature(): return 'worktree'\n"


# ── merge_failed pipeline status ─────────────────────────────────────────────

def test_merge_failed_state_exists() -> None:
    """The MERGE_FAILED state is defined in PipelineState."""
    from litehive.runner.states import PipelineState
    assert PipelineState.MERGE_FAILED.value == "merge_failed"


def test_merge_failed_routing() -> None:
    """commit_to_git fail routes to merge_failed in both route tables."""
    from litehive.runner.states import _ROUTES, _SINGLE_ROUTES
    assert _ROUTES[("commit_to_git", "fail")] == "merge_failed"
    assert _SINGLE_ROUTES[("commit_to_git", "fail")] == "merge_failed"


# ── Unmerged worktree tracking in state.yaml ──────────────────────────────────

def test_unmerged_worktree_recorded_in_state(tmp_path: Path) -> None:
    """When merge fails, the worktree is recorded in state.yaml unmerged_worktrees."""
    from litehive.tasks import get_task_worktree_path

    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Track unmerged worktree")

    worktree_path = _setup_worktree(tmp_path, task, feature_content="def feature(): return 'wt'\n")
    expected_wt_path = get_task_worktree_path(task)  # Use getter since save normalizes
    _create_conflict(tmp_path)

    subagents = _FailingSubagents(worktree_path)
    _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=subagents, config=LitehiveConfig(),
    )

    # Simulate the runner recording the unmerged worktree (what the runner does)
    from litehive.runner.core import _record_unmerged_worktree
    _record_unmerged_worktree(tmp_path, task)

    state = load_state(tmp_path)
    assert len(state.unmerged_worktrees) == 1
    assert state.unmerged_worktrees[0].task_id == task.id
    assert state.unmerged_worktrees[0].worktree_path == expected_wt_path


def test_unmerged_worktree_no_duplicate(tmp_path: Path) -> None:
    """Recording the same task twice should not duplicate the entry."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No duplicate")

    worktree_path = _setup_worktree(tmp_path, task)

    from litehive.runner.core import _record_unmerged_worktree
    _record_unmerged_worktree(tmp_path, task)
    _record_unmerged_worktree(tmp_path, task)

    state = load_state(tmp_path)
    assert len(state.unmerged_worktrees) == 1


def test_unmerged_worktree_persisted_to_yaml(tmp_path: Path) -> None:
    """Unmerged worktrees are persisted to the state.yaml file."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Persisted worktree")

    worktree_path = _setup_worktree(tmp_path, task)

    from litehive.runner.core import _record_unmerged_worktree
    _record_unmerged_worktree(tmp_path, task)

    # Read raw YAML to verify persistence
    state_file = tmp_path / ".litehive" / "state.yaml"
    raw = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert "unmerged_worktrees" in raw
    assert len(raw["unmerged_worktrees"]) == 1
    assert raw["unmerged_worktrees"][0]["task_id"] == task.id


# ── Status display ────────────────────────────────────────────────────────────

def test_status_shows_merge_failed(tmp_path: Path) -> None:
    """render_task_summary shows unmerged_worktree for merge_failed tasks."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Show merge_failed")

    task.status = "merge_failed"
    task.pipeline_status = "merge_failed"
    # Use runtime.git.worktree_path since save_task normalizes git.worktree_path away
    task.runtime.git.worktree_path = ".litehive/worktrees/T-0001-test"
    save_task(tmp_path, task)

    from litehive.observability import render_task_summary
    lines = render_task_summary(task, active=False, root=tmp_path)
    status_line = lines[0]
    assert "merge_failed" in status_line
    worktree_lines = [l for l in lines if "unmerged_worktree=" in l]
    assert len(worktree_lines) == 1
    assert ".litehive/worktrees/T-0001-test" in worktree_lines[0]
