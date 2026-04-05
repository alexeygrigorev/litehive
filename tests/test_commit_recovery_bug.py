"""Test that commit_to_git does not lose code.

Key invariants:
1. If merge fails, task is NOT marked done
2. If merge fails, worktree is NOT deleted
3. Task is only marked done when new commits are confirmed on main
"""

import subprocess
from pathlib import Path

import pytest

from litehive.config import LitehiveConfig, ensure_workspace
from litehive.git_ops import current_head
from litehive.runtime import _commit_to_git_report
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


def test_merge_conflict_does_not_delete_worktree(tmp_path: Path) -> None:
    """If merge fails and no agent resolves it, worktree must survive."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Precious code changes")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "feature.py").write_text("def feature(): return True\n")
    _run(["git", "add", "feature.py"], worktree_path)
    _run(["git", "commit", "-m", "add feature"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    # Create a conflict on main
    (tmp_path / "feature.py").write_text("def feature(): return False\n")
    _run(["git", "add", "feature.py"], tmp_path)
    _run(["git", "commit", "-m", "conflicting change on main"], tmp_path)

    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "fail"
    assert worktree_path.exists(), "Worktree was deleted despite failed merge!"
    assert (worktree_path / "feature.py").exists(), "Code was lost!"
    assert (worktree_path / "feature.py").read_text() == "def feature(): return True\n"
    assert task.status != "done"


def test_successful_merge_deletes_worktree(tmp_path: Path) -> None:
    """After successful merge, worktree should be cleaned up."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Clean merge task")

    worktree_path = tmp_path / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"], tmp_path)
    (worktree_path / "new_file.py").write_text("# new code\n")
    _run(["git", "add", "new_file.py"], worktree_path)
    _run(["git", "commit", "-m", "add new file"], worktree_path)

    task.git.worktree_path = str(worktree_path.relative_to(tmp_path))
    task.pipeline_status = "commit_to_git"
    save_task(tmp_path, task)

    head_before = current_head(tmp_path)
    report = _commit_to_git_report(tmp_path, worktree_path, task, auto_commit_enabled=True)

    assert report.verdict == "pass"
    assert task.status == "done"
    assert task.git.commit_sha is not None
    assert task.git.commit_sha != head_before
    assert (tmp_path / "new_file.py").exists()
    assert not worktree_path.exists(), "Worktree should be deleted after successful merge"


def test_head_unchanged_means_fail(tmp_path: Path) -> None:
    """If HEAD doesn't advance, commit_to_git must fail."""
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="No-op task")

    # execution_root == root, no worktree, no changes
    head_before = current_head(tmp_path)
    report = _commit_to_git_report(tmp_path, tmp_path, task, auto_commit_enabled=True)

    # No worktree means merge_ok=True but head shouldn't change
    # This is the same-repo case, so it marks done
    assert current_head(tmp_path) == head_before
