"""Tests for the worktree merge agent flow and merge_failed pipeline status.

Covers the four merge scenarios:
1. Rebase succeeds (clean merge, no agent)
2. Rebase fails, git merge succeeds (no agent needed)
3. Rebase fails, merge conflict, agent resolves
4. Rebase fails, merge conflict, agent fails

Also tests:
- merge_failed pipeline status existence and routing
- Unmerged worktree tracking in state.yaml
- Status display for merge_failed tasks
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from litehive.config import LitehiveConfig, ensure_workspace
from litehive.git_ops import current_head
from litehive.models import StageReport, SubagentRef, UnmergedWorktree
from litehive.observability import render_task_summary
from litehive.runner.states import PipelineState, _ROUTES, _SINGLE_ROUTES
from litehive.runtime import _commit_to_git_report
from litehive.subagents import SubagentResult
from litehive.tasks import create_task, load_state, save_state, save_task, set_task_worktree_path


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


def _setup_worktree(tmp_path: Path, task, *, filename="feature.py", content="def f(): return 'wt'\n"):
    """Create a detached worktree with one committed file."""
    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / filename).write_text(content)
    _run(["git", "add", filename], worktree_path)
    _run(["git", "commit", "-m", f"add {filename} in worktree"], worktree_path)
    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)
    return worktree_path


class _ResolvingSubagents:
    """Fake SubagentManager that resolves merge conflicts."""

    def __init__(self, root: Path):
        self._root = root

    def run(self, task, *, role, engine_name, prompt, model=None):
        # Resolve by picking worktree version and committing
        conflict_file = self._root / "feature.py"
        conflict_file.write_text("def f(): return 'wt'\n")
        _run(["git", "add", "feature.py"], self._root)
        _run(["git", "commit", "--no-edit"], self._root)
        return SubagentResult(
            ref=SubagentRef(id="SA-merge", role=role, engine=engine_name,
                            status="completed", path="subagents/SA-merge"),
            execution=None, transcript="Resolved conflict", exit_code=0,
        )


class _FailingSubagents:
    """Fake SubagentManager that fails to resolve merge conflicts."""

    def run(self, task, *, role, engine_name, prompt, model=None):
        # Agent runs but doesn't fix anything
        return SubagentResult(
            ref=SubagentRef(id="SA-merge", role=role, engine=engine_name,
                            status="completed", path="subagents/SA-merge"),
            execution=None, transcript="Could not resolve", exit_code=1,
        )


# ── Scenario 1: Rebase succeeds (clean merge, no agent) ───────────────────────

def test_rebase_succeeds_no_agent_needed(tmp_path: Path) -> None:
    """When worktree changes don't conflict, merge succeeds without an agent."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Clean merge")
    worktree_path = _setup_worktree(tmp_path, task, filename="new_file.py", content="# new\n")

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.pipeline_status == "done"
    assert (tmp_path / "new_file.py").exists()
    assert not worktree_path.exists()


# ── Scenario 2: Rebase fails, git merge succeeds ──────────────────────────────

def test_merge_succeeds_after_rebase_fail(tmp_path: Path) -> None:
    """When both sides change different files, merge succeeds without an agent."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Non-conflicting merge")
    worktree_path = _setup_worktree(tmp_path, task, filename="feature.py", content="def f(): return 'wt'\n")

    # Change a different file on main so merge has divergent history but no conflict
    (tmp_path / "other.py").write_text("# other change\n")
    _run(["git", "add", "other.py"], tmp_path)
    _run(["git", "commit", "-m", "other change on main"], tmp_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert (tmp_path / "feature.py").exists()
    assert (tmp_path / "other.py").exists()
    assert not worktree_path.exists()


# ── Scenario 3: Merge conflict, agent resolves ────────────────────────────────

def test_merge_conflict_agent_resolves(tmp_path: Path) -> None:
    """When merge conflicts and agent resolves, task completes and worktree is cleaned."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent resolves conflict")
    worktree_path = _setup_worktree(tmp_path, task)

    # Create conflict on main
    (tmp_path / "feature.py").write_text("def f(): return 'main'\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change"], tmp_path)

    report = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=_ResolvingSubagents(tmp_path), config=LitehiveConfig(),
    )

    assert report.verdict == "pass"
    assert task.status == "done"
    assert (tmp_path / "feature.py").read_text() == "def f(): return 'wt'\n"
    assert not worktree_path.exists()


# ── Scenario 4: Merge conflict, agent fails ───────────────────────────────────

def test_merge_conflict_agent_fails(tmp_path: Path) -> None:
    """When merge conflicts and agent fails, task fails with merge_conflict classification."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Agent fails to resolve")
    worktree_path = _setup_worktree(tmp_path, task)

    # Create conflict on main
    (tmp_path / "feature.py").write_text("def f(): return 'main'\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change"], tmp_path)

    report = _commit_to_git_report(
        tmp_path, worktree_path, task, auto_commit_enabled=True,
        subagents=_FailingSubagents(), config=LitehiveConfig(),
    )

    assert report.verdict == "fail"
    assert report.failure_classification == "merge_conflict"
    assert task.status != "done"
    assert worktree_path.exists()
    assert (worktree_path / "feature.py").read_text() == "def f(): return 'wt'\n"


# ── Part 2: merge_failed pipeline status ───────────────────────────────────────

def test_merge_failed_state_exists() -> None:
    """PipelineState.MERGE_FAILED enum value exists."""
    assert PipelineState.MERGE_FAILED == "merge_failed"
    assert PipelineState.MERGE_FAILED.value == "merge_failed"


def test_merge_failed_routing() -> None:
    """Both _ROUTES and _SINGLE_ROUTES map (commit_to_git, fail) to merge_failed."""
    assert _ROUTES[("commit_to_git", "fail")] == "merge_failed"
    assert _ROUTES[("commit_to_git", "reject")] == "merge_failed"
    assert _ROUTES[("commit_to_git", "blocked")] == "merge_failed"
    assert _SINGLE_ROUTES[("commit_to_git", "fail")] == "merge_failed"
    assert _SINGLE_ROUTES[("commit_to_git", "reject")] == "merge_failed"
    assert _SINGLE_ROUTES[("commit_to_git", "blocked")] == "merge_failed"


# ── Part 3: Unmerged worktree tracking ─────────────────────────────────────────

def test_unmerged_worktree_recorded_in_state(tmp_path: Path) -> None:
    """When merge fails, the worktree is recorded in state.yaml via _record_unmerged_worktree."""
    from litehive.runner.core import _record_unmerged_worktree

    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Unmerged tracking")
    task.git.worktree_path = ".litehive/worktrees/T-0001-test"
    save_task(tmp_path, task)

    _record_unmerged_worktree(tmp_path, task)

    state = load_state(tmp_path)
    assert len(state.unmerged_worktrees) == 1
    assert state.unmerged_worktrees[0].task_id == task.id
    assert state.unmerged_worktrees[0].worktree_path == ".litehive/worktrees/T-0001-test"


def test_unmerged_worktree_no_duplicate(tmp_path: Path) -> None:
    """Recording the same task twice doesn't create a duplicate entry."""
    from litehive.runner.core import _record_unmerged_worktree

    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Duplicate guard")
    task.git.worktree_path = ".litehive/worktrees/T-0001-dup"
    save_task(tmp_path, task)

    _record_unmerged_worktree(tmp_path, task)
    _record_unmerged_worktree(tmp_path, task)

    state = load_state(tmp_path)
    assert len(state.unmerged_worktrees) == 1


def test_unmerged_worktree_persisted_to_yaml(tmp_path: Path) -> None:
    """Raw YAML file contains the unmerged_worktrees field."""
    from litehive.runner.core import _record_unmerged_worktree

    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="YAML persistence")
    task.git.worktree_path = ".litehive/worktrees/T-0001-yaml"
    save_task(tmp_path, task)

    _record_unmerged_worktree(tmp_path, task)

    raw = yaml.safe_load((tmp_path / ".litehive" / "state.yaml").read_text(encoding="utf-8"))
    assert "unmerged_worktrees" in raw
    assert len(raw["unmerged_worktrees"]) == 1
    assert raw["unmerged_worktrees"][0]["task_id"] == task.id


# ── Status display ─────────────────────────────────────────────────────────────

def test_status_shows_merge_failed(tmp_path: Path) -> None:
    """render_task_summary shows unmerged_worktree for merge_failed tasks."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Status display")
    task.status = "merge_failed"
    task.pipeline_status = "merge_failed"
    set_task_worktree_path(task, ".litehive/worktrees/T-0001-status")
    save_task(tmp_path, task)

    lines = render_task_summary(task, active=False, root=tmp_path)
    text = "\n".join(lines)

    assert "merge_failed" in text
    assert "unmerged_worktree=" in text
    assert ".litehive/worktrees/T-0001-status" in text
