"""Git integration helpers."""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from litehive.domain.task import TaskRecord

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_SUBJECT_TEMPLATE = "litehive: complete {task_id} {slug}"
CHECKPOINT_ATTEMPT_SUFFIX_TEMPLATE = "{base} (attempt {attempt})"
ROLLBACK_SUBJECT_TEMPLATE = "litehive: rollback {task_id} {slug} (attempt {attempt})"


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


def has_non_litehive_changes(root: Path) -> bool:
    for line in status_porcelain(root):
        path = line[3:] if len(line) > 3 else ""
        if path and not path.startswith(".litehive/"):
            return True
    return False


def current_head(root: Path) -> str | None:
    proc = _run_git(root, "rev-parse", "--verify", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def add_worktree(root: Path, path: Path, *, ref: str = "HEAD") -> None:
    proc = _run_git(root, "worktree", "add", "--detach", str(path), ref)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree add failed")


def move_worktree(root: Path, source: Path, destination: Path) -> None:
    proc = _run_git(root, "worktree", "move", str(source), str(destination))
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree move failed")


def prune_worktrees(root: Path) -> None:
    proc = _run_git(root, "worktree", "prune")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree prune failed")


def merge_commit(root: Path, commit_sha: str) -> str:
    proc = _run_git(root, "merge", "--ff-only", commit_sha)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git merge --ff-only failed")
    head = current_head(root)
    if head is None:
        raise GitError("git merge completed but HEAD could not be resolved")
    return head


def is_ancestor(root: Path, ancestor_sha: str, descendant_sha: str) -> bool:
    proc = _run_git(root, "merge-base", "--is-ancestor", ancestor_sha, descendant_sha)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError(proc.stderr.strip() or "git merge-base --is-ancestor failed")


def remove_worktree(root: Path, path: Path, *, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    proc = _run_git(root, *args)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree remove failed")


def rebase_worktree_onto(worktree: Path, target_ref: str) -> bool:
    """Rebase uncommitted-clean worktree HEAD onto *target_ref*.

    Stashes any dirty changes, rebases, then re-applies the stash.
    Returns True if the rebase succeeded, False if it conflicted
    (in which case the worktree is left unchanged).
    """
    had_changes = has_changes(worktree)
    if had_changes:
        stash = _run_git(worktree, "stash", "push", "-u", "-m", "litehive-rebase-temp")
        if stash.returncode != 0:
            return False

    rebase = _run_git(worktree, "rebase", target_ref)
    if rebase.returncode != 0:
        _run_git(worktree, "rebase", "--abort")
        if had_changes:
            _run_git(worktree, "stash", "pop")
        return False

    if had_changes:
        pop = _run_git(worktree, "stash", "pop")
        if pop.returncode != 0:
            # Stash conflicts with rebased state — undo the rebase too
            _run_git(worktree, "rebase", "--abort")
            return False

    return True


def cherry_pick_commit(root: Path, commit_sha: str) -> str:
    proc = _run_git(root, "cherry-pick", commit_sha)
    if proc.returncode != 0:
        abort = _run_git(root, "cherry-pick", "--abort")
        if abort.returncode != 0:
            raise GitError(
                (proc.stderr.strip() or "git cherry-pick failed")
                + f"; additionally failed to abort cherry-pick: {abort.stderr.strip() or 'unknown error'}"
            )
        raise GitError(proc.stderr.strip() or "git cherry-pick failed")
    head = current_head(root)
    if head is None:
        raise GitError("git cherry-pick completed but HEAD could not be resolved")
    return head


def default_commit_message(task_id: str, slug: str) -> str:
    return DEFAULT_CHECKPOINT_SUBJECT_TEMPLATE.format(task_id=task_id, slug=slug)


def _uses_generated_commit_message(task: TaskRecord) -> bool:
    message = task.git.commit_message
    if message is None:
        return True
    return message == default_commit_message(task.id, task.slug)


def checkpoint_message(task: TaskRecord, attempt: int | None = None) -> str:
    """Return the deterministic checkpoint subject for the next or requested attempt."""
    base = task.git.commit_message or default_commit_message(task.id, task.slug)
    attempt = attempt or (task.git.checkpoint_attempts + 1)
    if attempt > 1 and _uses_generated_commit_message(task):
        return CHECKPOINT_ATTEMPT_SUFFIX_TEMPLATE.format(base=base, attempt=attempt)
    return base


def rollback_message(task: TaskRecord, attempt: int) -> str:
    return ROLLBACK_SUBJECT_TEMPLATE.format(task_id=task.id, slug=task.slug, attempt=attempt)


def find_commit_by_subject(root: Path, subject: str) -> str | None:
    proc = _run_git(root, "log", "--format=%H%x00%s")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git log failed")

    for line in proc.stdout.splitlines():
        sha, _, message = line.partition("\x00")
        if message == subject:
            return sha
    return None


def commit_task(root: Path, message: str, *, paths: list[str] | None = None) -> CommitCheckpoint | None:
    if not is_git_repo(root):
        return None

    base_sha = current_head(root)
    if paths:
        add_proc = _run_git(root, "add", "--", *paths)
    else:
        if not has_changes(root):
            return None
        add_proc = _run_git(root, "add", "-A")
    if add_proc.returncode != 0:
        raise GitError(add_proc.stderr.strip() or "git add failed")

    staged_proc = _run_git(root, "diff", "--cached", "--quiet", "--exit-code")
    if staged_proc.returncode == 0:
        return None
    if staged_proc.returncode not in {0, 1}:
        raise GitError(staged_proc.stderr.strip() or "git diff --cached failed")

    commit_proc = _run_git(root, "commit", "-m", message)
    if commit_proc.returncode != 0:
        raise GitError(commit_proc.stderr.strip() or "git commit failed")

    rev_proc = _run_git(root, "rev-parse", "HEAD")
    if rev_proc.returncode != 0:
        raise GitError(rev_proc.stderr.strip() or "git rev-parse failed")
    return CommitCheckpoint(commit_sha=rev_proc.stdout.strip(), base_sha=base_sha, message=message)


def rollback_task(root: Path, task: TaskRecord) -> RollbackCheckpoint:
    if has_non_litehive_changes(root):
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

    # Try plain revert first; if the commit is a merge, retry with -m 1
    revert_proc = _run_git(root, "revert", "--no-commit", checkpoint_sha)
    if revert_proc.returncode != 0:
        if "is a merge" in (revert_proc.stderr or ""):
            _run_git(root, "reset", "--hard", "HEAD")  # clean up failed revert
            revert_proc = _run_git(root, "revert", "--no-commit", "-m", "1", checkpoint_sha)
        if revert_proc.returncode != 0:
            raise GitError(revert_proc.stderr.strip() or "git revert failed")
    return RollbackCheckpoint(rolled_back_sha=checkpoint_sha)


def abort_revert(root: Path) -> None:
    proc = _run_git(root, "revert", "--abort")
    if proc.returncode == 0:
        return

    for _ in range(16):
        conflict_paths = re.findall(
            r"Untracked working tree file '([^']+)' would be overwritten by merge\.",
            proc.stderr,
        )
        if not conflict_paths:
            break
        for relative_path in conflict_paths:
            conflict_path = root / relative_path
            if conflict_path.is_dir():
                try:
                    shutil.rmtree(conflict_path)
                except OSError as exc:
                    logger.warning("Failed to remove conflict path %s: %s", conflict_path, exc)
            elif conflict_path.exists():
                conflict_path.unlink()
        proc = _run_git(root, "revert", "--abort")
        if proc.returncode == 0:
            return

    raise GitError(proc.stderr.strip() or "git revert --abort failed")
