"""Git integration helpers."""

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from litehive.domain.task import TaskRecord
from litehive.fs_cleanup import remove_tree_logged

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_SUBJECT_TEMPLATE = "litehive: complete {task_id} {slug}"
CHECKPOINT_ATTEMPT_SUFFIX_TEMPLATE = "{base} (attempt {attempt})"
ROLLBACK_SUBJECT_TEMPLATE = "litehive: rollback {task_id} {slug} (attempt {attempt})"
COMPLETION_SUBJECT_TEMPLATE = "litehive {task_id}: {title}"


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


def rev_parse_verify(cwd: Path, ref: str) -> str | None:
    """Return the sha for ``ref`` (``git rev-parse --verify <ref>``).

    Returns ``None`` when the ref does not resolve. Used by the
    daemon's origin-divergence check to compare ``main`` against
    ``origin/main``.
    """
    proc = _run_git(cwd, "rev-parse", "--verify", ref)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def list_remote_names(cwd: Path) -> list[str]:
    """Return the configured remote names (``git remote``).

    Returns an empty list if the call fails (e.g. not a git repo).
    """
    proc = _run_git(cwd, "remote")
    if proc.returncode != 0:
        return []
    return [name for name in proc.stdout.split() if name]


def fetch(cwd: Path, remote: str, *refs: str) -> tuple[bool, str]:
    """Best-effort ``git fetch <remote> <refs...>``.

    Returns ``(success, stderr)``. Network issues are intentionally
    not raised — the daemon treats fetch failure as "no opinion"
    rather than a halt condition.
    """
    proc = _run_git(cwd, "fetch", remote, *refs)
    return proc.returncode == 0, proc.stderr.strip()


def path_differs_at_ref(cwd: Path, ref: str, path: str) -> bool:
    """Return whether ``path`` differs between the worktree and ``ref``.

    Wraps ``git diff --quiet <ref> -- <path>`` and translates the
    exit code: 0 = identical, 1 = differs, anything else = error.
    """
    proc = _run_git(cwd, "diff", "--quiet", ref, "--", path)
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    raise GitError(proc.stderr.strip() or f"git diff --quiet failed for {path}")


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


def diff_name_status(cwd: Path, *args: str) -> list[tuple[str, str]]:
    """Return ``[(status, path), ...]`` for ``git diff --name-status <args>``.

    ``status`` is the single-letter git diff code (``M``, ``A``,
    ``D``, ``R``, …). Used by recovery/scope_analysis to enumerate
    files added or deleted between branches.
    """
    proc = _run_git(cwd, "diff", "--name-status", *args)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git diff --name-status failed")
    out: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        if not path:
            continue
        out.append((status, path))
    return out


def path_exists_in_ref(cwd: Path, ref: str, path: str) -> bool:
    """Return whether ``ref:path`` resolves to an object.

    Wraps ``git cat-file -e <ref>:<path>``. The recovery
    scope-analysis flow uses this to ask "did this file exist on
    main when the worktree branched off?".
    """
    proc = _run_git(cwd, "cat-file", "-e", f"{ref}:{path}")
    return proc.returncode == 0


def show_at_ref(cwd: Path, ref: str, path: str) -> str:
    """Return the contents of ``path`` at ``ref`` (``git show ref:path``).

    Raises :class:`GitError` when the path is not present in the ref.
    """
    proc = _run_git(cwd, "show", f"{ref}:{path}")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git show {ref}:{path} failed")
    return proc.stdout


def checkout_ref(cwd: Path, ref: str) -> bool:
    """Best-effort ``git checkout <ref>``. Returns success.

    Used in the recovery scope-analysis flow which temporarily
    switches branches to run a check, then switches back. The
    caller is expected to handle the false return.
    """
    return _run_git(cwd, "checkout", ref).returncode == 0


def stash_push(cwd: Path, message: str) -> bool:
    """Best-effort ``git stash push -m <message>``. Returns success."""
    return _run_git(cwd, "stash", "push", "-m", message).returncode == 0


def stash_pop(cwd: Path) -> bool:
    """Best-effort ``git stash pop``. Returns success."""
    return _run_git(cwd, "stash", "pop").returncode == 0


def merge_no_edit(cwd: Path, ref: str) -> tuple[bool, str]:
    """Run ``git merge <ref> --no-edit`` in ``cwd``.

    Returns ``(success, stderr_or_stdout)``. Used by the
    merge-resolver agent flow which needs to distinguish a clean
    merge from a conflict (and surface the message either way).
    """
    proc = _run_git(cwd, "merge", ref, "--no-edit")
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def merge_abort(cwd: Path) -> None:
    """Best-effort ``git merge --abort`` in ``cwd``.

    Used after a conflicting merge could not be resolved (either
    automatically or by the merge-resolver agent). Failure is
    intentionally silent — we are already on the failure path and
    have nothing more to recover.
    """
    _run_git(cwd, "merge", "--abort")


def unmerged_files(cwd: Path) -> list[str]:
    """Return paths with merge conflicts (``--diff-filter=U``).

    Called after ``merge_no_edit`` returns a non-success result to
    enumerate the files the merge-resolver agent needs to fix.
    """
    proc = _run_git(cwd, "diff", "--name-only", "--diff-filter=U")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


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


def _clean_commit_text(value: str) -> str:
    lines = [line.rstrip() for line in value.strip().splitlines()]
    return "\n".join(lines).strip()


def _metadata_body(task: TaskRecord) -> list[str]:
    lines = [
        f"Task: {task.id}",
        f"Title: {task.title}",
    ]
    goal = _clean_commit_text(task.goal)
    if goal:
        lines.extend(["", "Goal:", goal])
    if task.acceptance_criteria:
        lines.extend(["", "Acceptance criteria:"])
        lines.extend(f"- {_clean_commit_text(item)}" for item in task.acceptance_criteria if _clean_commit_text(item))
    return lines


def generated_completion_commit_message(task: TaskRecord, *, detail: str | None = None) -> str:
    """Return LiteHive's generated completion commit message for a task.

    The subject stays compact for ``git log --oneline`` while the body carries
    the persisted task metadata that explains what the completion represents.
    """
    subject = COMPLETION_SUBJECT_TEMPLATE.format(task_id=task.id, title=task.title)
    lines = [subject, "", *_metadata_body(task)]
    if detail:
        lines.extend(["", f"Commit detail: {detail}"])
    return "\n".join(lines)


def _with_attempt_suffix(message: str, attempt: int) -> str:
    subject, separator, body = message.partition("\n")
    subject = CHECKPOINT_ATTEMPT_SUFFIX_TEMPLATE.format(base=subject, attempt=attempt)
    return subject + separator + body


def _uses_generated_commit_message(task: TaskRecord) -> bool:
    message = task.git.commit_message
    if message is None:
        return True
    return message == default_commit_message(task.id, task.slug)


def checkpoint_message(task: TaskRecord, attempt: int | None = None) -> str:
    """Return the deterministic checkpoint subject for the next or requested attempt."""
    if _uses_generated_commit_message(task):
        base = generated_completion_commit_message(task)
    else:
        base = task.git.commit_message or default_commit_message(task.id, task.slug)
    attempt = attempt or (task.git.checkpoint_attempts + 1)
    if attempt > 1 and _uses_generated_commit_message(task):
        return _with_attempt_suffix(base, attempt)
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
                    remove_tree_logged(conflict_path, logger=logger, target_label="git conflict directory")
                except OSError as exc:
                    logger.warning("Failed to remove conflict path %s: %s", conflict_path, exc)
            elif conflict_path.exists():
                conflict_path.unlink()
        proc = _run_git(root, "revert", "--abort")
        if proc.returncode == 0:
            return

    raise GitError(proc.stderr.strip() or "git revert --abort failed")
