"""Test that commit_to_git recovery does NOT mark task done when merge fails.

Reproduces the bug where:
1. commit_to_git fails (e.g. untracked files conflict)
2. Recovery agent runs but doesn't actually merge the code
3. _attempt_commit_recovery returns the existing HEAD SHA (unchanged)
4. Task is marked done and worktree is deleted
5. All code changes are irrecoverably lost
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from litehive.config import LitehiveConfig, ensure_workspace
from litehive.git_ops import current_head
from litehive.runtime import _attempt_commit_recovery, _commit_to_git_report
from litehive.tasks import create_task, save_task, task_dir


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


def test_recovery_must_not_mark_done_when_merge_did_not_happen(tmp_path: Path) -> None:
    """Recovery agent runs but doesn't merge. Task must NOT be marked done."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Code that must not be lost")

    # Create worktree with actual code changes
    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "important_feature.py").write_text("# critical code\ndef important(): pass\n")
    _run(["git", "add", "important_feature.py"], worktree_path)
    _run(["git", "commit", "-m", "implement important feature"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)

    # Simulate: recovery agent runs but does NOT merge (just returns without doing anything)
    head_before = current_head(tmp_path)
    recovery_sha = _attempt_commit_recovery(
        tmp_path,
        worktree_path,
        task,
        "untracked working tree files would be overwritten by merge",
        subagents=None,  # No real agent - simulates agent that does nothing
    )

    # Recovery returned None because no subagents - that's correct
    assert recovery_sha is None

    # HEAD must not have changed
    assert current_head(tmp_path) == head_before

    # Task must NOT be done
    assert task.status != "done"

    # Worktree must still exist
    assert worktree_path.exists()
    assert (worktree_path / "important_feature.py").exists()


def test_commit_to_git_does_not_delete_worktree_on_failed_merge(tmp_path: Path) -> None:
    """If merge fails and recovery fails, the worktree must survive."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Precious code changes")

    # Create worktree with code
    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "feature.py").write_text("def feature(): return True\n")
    _run(["git", "add", "feature.py"], worktree_path)
    _run(["git", "commit", "-m", "add feature"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    # Create a conflict on main so merge will fail
    (tmp_path / "feature.py").write_text("def feature(): return False  # different\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change on main"], tmp_path)

    # Run commit_to_git - it should fail but NOT delete the worktree
    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    # The merge should fail (conflict + no subagents for recovery)
    assert report.verdict == "fail"

    # CRITICAL: worktree must still exist with the code
    assert worktree_path.exists(), "Worktree was deleted despite failed merge!"
    assert (worktree_path / "feature.py").exists(), "Code was lost!"
    assert (worktree_path / "feature.py").read_text() == "def feature(): return True\n"


def test_recovery_returns_none_when_agent_runs_but_head_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The core bug: recovery agent runs, does nothing, but _attempt_commit_recovery
    returns the existing HEAD as if the merge succeeded."""
    initial_sha = _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Code must survive failed recovery")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "critical.py").write_text("# must not be lost\n")
    _run(["git", "add", "critical.py"], worktree_path)
    _run(["git", "commit", "-m", "critical work"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    save_task(tmp_path, task)

    head_before = current_head(tmp_path)

    # Fake subagent manager that does nothing (agent runs but doesn't fix anything)
    class FakeSubagents:
        def __init__(self):
            self.execution_root = worktree_path

        def run(self, task, *, role, engine_name, prompt, model=None):
            from litehive.subagents import SubagentResult
            from litehive.models import SubagentRef

            # Agent "runs" but doesn't merge anything
            return SubagentResult(
                ref=SubagentRef(
                    id="SA-fake",
                    role=role,
                    engine=engine_name,
                    status="completed",
                    path="subagents/SA-fake",
                ),
                execution=None,
                transcript="I tried but couldn't fix it",
                exit_code=0,
            )

    recovery_sha = _attempt_commit_recovery(
        tmp_path,
        worktree_path,
        task,
        "untracked files conflict",
        subagents=FakeSubagents(),
        config=LitehiveConfig(),
    )

    # HEAD did not change - recovery must return None
    assert current_head(tmp_path) == head_before
    assert recovery_sha is None, (
        f"Recovery returned {recovery_sha} but HEAD didn't change! "
        f"This would cause the task to be marked done and the worktree deleted, losing all code."
    )
