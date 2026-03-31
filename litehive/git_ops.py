"""Git integration helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from litehive.models import TaskRecord


class GitError(RuntimeError):
    """Raised when git operations fail."""


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def is_git_repo(root: Path) -> bool:
    proc = _run_git(root, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def has_changes(root: Path) -> bool:
    proc = _run_git(root, "status", "--porcelain")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def commit_task(root: Path, task: TaskRecord) -> str | None:
    if not is_git_repo(root) or not has_changes(root):
        return None

    add_proc = _run_git(root, "add", "-A")
    if add_proc.returncode != 0:
        raise GitError(add_proc.stderr.strip() or "git add failed")

    message = task.git.commit_message or f"litehive: complete {task.id} {task.slug}"
    commit_proc = _run_git(root, "commit", "-m", message)
    if commit_proc.returncode != 0:
        raise GitError(commit_proc.stderr.strip() or "git commit failed")

    rev_proc = _run_git(root, "rev-parse", "HEAD")
    if rev_proc.returncode != 0:
        raise GitError(rev_proc.stderr.strip() or "git rev-parse failed")
    return rev_proc.stdout.strip()
