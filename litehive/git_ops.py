"""Git integration helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from litehive.models import TaskRecord


class GitError(RuntimeError):
    """Raised when git operations fail."""


@dataclass(slots=True)
class CommitCheckpoint:
    commit_sha: str
    base_sha: str | None
    message: str


@dataclass(slots=True)
class RollbackCheckpoint:
    rolled_back_sha: str


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


def status_porcelain(root: Path) -> list[str]:
    proc = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git status failed")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def current_head(root: Path) -> str | None:
    proc = _run_git(root, "rev-parse", "--verify", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def checkpoint_message(task: TaskRecord, attempt: int | None = None) -> str:
    base = task.git.commit_message or f"litehive: checkpoint {task.id} {task.slug}"
    attempt = attempt or (task.git.checkpoint_attempts + 1)
    if attempt > 1 and task.git.commit_message is None:
        return f"{base} (attempt {attempt})"
    return base


def rollback_message(task: TaskRecord, attempt: int) -> str:
    return f"litehive: rollback {task.id} {task.slug} (attempt {attempt})"


def find_commit_by_subject(root: Path, subject: str) -> str | None:
    proc = _run_git(root, "log", "--format=%H%x00%s")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git log failed")

    for line in proc.stdout.splitlines():
        sha, _, message = line.partition("\x00")
        if message == subject:
            return sha
    return None


def commit_task(root: Path, message: str) -> CommitCheckpoint | None:
    if not is_git_repo(root) or not has_changes(root):
        return None

    base_sha = current_head(root)
    add_proc = _run_git(root, "add", "-A")
    if add_proc.returncode != 0:
        raise GitError(add_proc.stderr.strip() or "git add failed")

    commit_proc = _run_git(root, "commit", "-m", message)
    if commit_proc.returncode != 0:
        raise GitError(commit_proc.stderr.strip() or "git commit failed")

    rev_proc = _run_git(root, "rev-parse", "HEAD")
    if rev_proc.returncode != 0:
        raise GitError(rev_proc.stderr.strip() or "git rev-parse failed")
    return CommitCheckpoint(commit_sha=rev_proc.stdout.strip(), base_sha=base_sha, message=message)


def rollback_task(root: Path, task: TaskRecord) -> RollbackCheckpoint:
    if has_changes(root):
        raise GitError("Workspace has uncommitted changes; rollback requires a clean worktree")
    if task.git.checkpoint_attempts < 1:
        raise GitError(f"Task {task.id} has no checkpoint commit to roll back")
    if not is_git_repo(root):
        raise GitError("Workspace is not a git repository")

    checkpoint_sha = find_commit_by_subject(
        root,
        checkpoint_message(task, attempt=task.git.checkpoint_attempts),
    )
    if checkpoint_sha is None:
        raise GitError(f"Unable to locate checkpoint commit for task {task.id}")

    revert_proc = _run_git(root, "revert", "--no-commit", checkpoint_sha)
    if revert_proc.returncode != 0:
        raise GitError(revert_proc.stderr.strip() or "git revert failed")
    return RollbackCheckpoint(rolled_back_sha=checkpoint_sha)
