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
    """Run a git subprocess against ``root`` capturing stdout/stderr without raising.

    The shared bottom of every wrapper in this module: callers branch on
    ``returncode`` and ``stderr`` to translate git failures into domain
    errors, so we never let CalledProcessError leak past the git layer.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def is_git_repo(root: Path) -> bool:
    """Cheap probe used at every git-touching entry point to short-circuit when the workspace isn't a checkout yet."""
    proc = _run_git(root, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def has_changes(root: Path) -> bool:
    """Quick "is the worktree dirty at all?" check used by the rebase helper before stashing."""
    proc = _run_git(root, "status", "--porcelain")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def status_porcelain(root: Path) -> list[str]:
    """Full porcelain listing including all untracked files; raises so callers see the workspace truthfully rather than a silent empty list."""
    proc = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git status failed")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def has_non_litehive_changes(root: Path) -> bool:
    """Tells the runner/rollback path whether the user has unrelated edits in the worktree that we must not steamroll, ignoring our own ``.litehive/`` metadata churn."""
    for line in status_porcelain(root):
        if len(line) > 3:
            path = line[3:]
        else:
            path = ""
        if path and not path.startswith(".litehive/"):
            return True
    return False


def current_head(root: Path) -> str | None:
    """Soft HEAD lookup that returns ``None`` for an unborn repo; the strict counterpart :func:`head_sha_strict` raises instead."""
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


def delete_branch(cwd: Path, branch: str) -> None:
    """Best-effort ``git branch -D <branch>`` in ``cwd``.

    Used after a managed worktree is removed: the runner cleans up
    the branch ref so it doesn't shadow a future task with the same
    slug. Ignores failures (e.g. branch already gone) — the call
    site does not act on the result.
    """
    _run_git(cwd, "branch", "-D", branch)


def remote_url(cwd: Path, remote: str = "origin") -> str | None:
    """Return the URL configured for ``remote``, or ``None`` if missing.

    Wraps ``git remote get-url <remote>``. Used by the worktree
    code to ask "does this checkout have an origin?" without
    surfacing a noisy ``GitError`` on the missing-remote path.
    """
    proc = _run_git(cwd, "remote", "get-url", remote)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def head_sha_strict(cwd: Path) -> str:
    """Return ``HEAD`` sha; raise :class:`GitError` if it can't be read.

    The non-strict counterpart is :func:`current_head`. Use this when
    the caller treats a missing HEAD as a hard error rather than a
    "not initialized yet" signal.
    """
    proc = _run_git(cwd, "rev-parse", "HEAD")
    if proc.returncode != 0:
        raise GitError(f"cannot read HEAD at {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def current_branch(cwd: Path) -> str | None:
    """Return the current branch name, or ``None`` for a detached HEAD.

    Wraps ``git symbolic-ref --quiet --short HEAD``. Used by the
    runner to know which branch a worktree is checked out on.
    """
    proc = _run_git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def cherry_pick_no_commit(cwd: Path, sha: str) -> tuple[bool, str]:
    """Run ``git cherry-pick --no-commit <sha>``.

    Returns ``(success, stderr_or_stdout)``. Used by the worktree
    rescue flow which wants to inspect the staged result before
    committing.
    """
    proc = _run_git(cwd, "cherry-pick", "--no-commit", sha)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def cherry_pick_abort(cwd: Path) -> None:
    """Best-effort ``git cherry-pick --abort``. Silent on failure."""
    _run_git(cwd, "cherry-pick", "--abort")


def index_has_staged_changes(cwd: Path) -> bool:
    """Return whether the index has staged changes ready to commit.

    Wraps ``git diff --cached --quiet --exit-code``. Translates
    git's exit codes: 0 = no diff, 1 = differs, anything else =
    error (raises :class:`GitError`).
    """
    proc = _run_git(cwd, "diff", "--cached", "--quiet", "--exit-code")
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    raise GitError(f"git diff --cached failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")


def commit_reuse_message(cwd: Path, sha: str) -> tuple[bool, str]:
    """Run ``git commit --reuse-message=<sha>``.

    Returns ``(success, stderr_or_stdout)``. Used by the rescue
    flow when re-applying a cherry-picked commit so the message
    metadata stays intact.
    """
    proc = _run_git(cwd, "commit", "--reuse-message", sha)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def cherry_check(cwd: Path, upstream_sha: str, head_sha: str) -> list[str] | None:
    """Run ``git cherry <upstream> <head>`` and return the marker lines.

    Returns ``None`` when the call fails (non-zero exit). On success
    each entry is ``+ <sha>`` (commit needs to land) or ``- <sha>``
    (already in upstream). Used by the worktree-rescue path to ask
    "are this branch's commits already on main?".
    """
    proc = _run_git(cwd, "cherry", upstream_sha, head_sha)
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def status_porcelain_with_options(cwd: Path, include_ignored: bool = False) -> list[str]:
    """``git status --porcelain`` with optional ``--ignored --untracked-files=all``.

    Used by the runner's auto-commit code to enumerate dirty entries.
    Raises :class:`GitError` on git failure.
    """
    args = ["status", "--porcelain"]
    if include_ignored:
        args.extend(["--ignored", "--untracked-files=all"])
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        raise GitError(f"git status failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def add_paths(cwd: Path, paths: list[str], all_flag: bool = False) -> None:
    """Run ``git add [--all] -- <paths>`` in ``cwd``.

    Raises :class:`GitError` if the add fails. ``all_flag`` controls
    whether ``--all`` is passed (used for cleanup commits where new
    files should be staged); without it, only paths explicitly named
    are staged.
    """
    args = ["add"]
    if all_flag:
        args.append("--all")
    args.extend(["--", *paths])
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        raise GitError(f"git add failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")


def commit_with_message_stdin(cwd: Path, message: str) -> None:
    """Commit currently-staged changes with ``message`` piped via stdin.

    Wraps ``git commit -F -``. Used when the runner builds a
    multi-line commit body that includes characters which would be
    awkward on the command line. Raises :class:`GitError` on failure.
    """
    proc = subprocess.run(
        ["git", "commit", "-F", "-"],
        cwd=str(cwd),
        input=message,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"git commit failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")


def commit_no_edit(cwd: Path) -> None:
    """Conclude an in-progress merge with ``git commit --no-edit``.

    Raises :class:`GitError` on failure. Used when a merge is
    already partially applied and the runner just needs to seal it.
    """
    proc = _run_git(cwd, "commit", "--no-edit")
    if proc.returncode != 0:
        raise GitError(
            f"git commit --no-edit failed in {cwd}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def is_path_tracked(cwd: Path, path: str) -> bool:
    """Return whether ``path`` is currently tracked in the index.

    Wraps ``git ls-files --error-unmatch -- <path>``. Used when
    filtering paths the caller wants to ``git add`` — a
    no-longer-existing path that's still tracked is safe to add
    (it'll be staged as a deletion); a no-longer-existing untracked
    path would error out the whole batch.
    """
    return _run_git(cwd, "ls-files", "--error-unmatch", "--", path).returncode == 0


def check_ignore(cwd: Path, path: str) -> bool:
    """Return whether ``path`` is ignored under ``.gitignore`` rules.

    Wraps ``git check-ignore --quiet --no-index -- <path>``. The
    ``--no-index`` flag asks git to evaluate ignore rules even for
    paths that are tracked (e.g. old task report artifacts the
    runner now wants to skip from a fresh ``git add``). Raises
    :class:`GitError` for unexpected exit codes.
    """
    proc = _run_git(cwd, "check-ignore", "--quiet", "--no-index", "--", path)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError(f"git check-ignore failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")


def stdout_or_none(cwd: Path, *args: str) -> str | None:
    """Run ``git <args>`` and return stripped stdout, or ``None`` on failure.

    Used by worktree helpers that ask git read-only questions where
    "the call failed" and "the answer is empty" should both collapse
    to ``None`` for the caller's convenience (e.g.
    ``git rev-parse``, ``git branch --show-current``,
    ``git merge-base``).
    """
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def stdout_lines(cwd: Path, *args: str) -> list[str]:
    """Run ``git <args>`` and return non-empty stripped output lines.

    Returns an empty list on failure or empty output. Used by
    worktree helpers that ask for path or revision lists
    (``git diff --name-only``, ``git rev-list``, etc.).
    """
    value = stdout_or_none(cwd, *args)
    if value is None:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


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


def add_worktree(root: Path, path: Path, ref: str = "HEAD") -> None:
    """Create a detached-HEAD worktree at ``path`` for the worktree pool used by the runner; ``--detach`` is deliberate so the parent branch isn't moved when the runner experiments."""
    proc = _run_git(root, "worktree", "add", "--detach", str(path), ref)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree add failed")


def move_worktree(root: Path, source: Path, destination: Path) -> None:
    """Relocate a worktree directory atomically via git so the worktree registry stays consistent with the new path."""
    proc = _run_git(root, "worktree", "move", str(source), str(destination))
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree move failed")


def prune_worktrees(root: Path, expire_now: bool = False) -> None:
    """Run ``git worktree prune``, optionally with ``--expire now``.

    ``expire_now=True`` forces git to garbage-collect stale worktree
    directories immediately rather than after the configured grace
    period; the runner uses it before re-adding a worktree at the
    same path.
    """
    args = ["worktree", "prune"]
    if expire_now:
        args.extend(["--expire", "now"])
    proc = _run_git(root, *args)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree prune failed")


def add_worktree_branch(root: Path, branch: str, path: Path, ref: str = "HEAD", force: bool = False) -> None:
    """Create a new worktree for ``branch`` at ``path``.

    Wraps ``git worktree add [-f] -B <branch> <path> <ref>``. ``-B``
    creates or resets the branch; ``--force`` lets the call succeed
    even when ``path`` already exists in the worktree list (used by
    the runner when it has already cleaned up the directory).
    """
    args = ["worktree", "add"]
    if force:
        args.append("--force")
    args.extend(["-B", branch, str(path), ref])
    proc = _run_git(root, *args)
    if proc.returncode != 0:
        raise GitError(f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}")


def list_worktrees_porcelain(root: Path) -> str:
    """Return the raw ``git worktree list --porcelain`` output.

    The caller is expected to parse the porcelain blocks. Raises
    :class:`GitError` on failure.
    """
    proc = _run_git(root, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        raise GitError(f"git worktree list failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def merge_commit(root: Path, commit_sha: str) -> str:
    """Fast-forward main onto ``commit_sha``; ``--ff-only`` enforces the invariant that worktree commits always replay linearly onto main."""
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


def stash_push(
    cwd: Path,
    message: str,
    include_untracked: bool = False,
    paths: list[str] | None = None,
) -> tuple[bool, str]:
    """Run ``git stash push -m <message>`` (optionally ``-u`` and path-scoped).

    Returns ``(success, stderr_or_stdout)``. Untracked files are
    included via ``-u`` when the caller asks; the optional
    ``paths`` argument scopes the stash to specific pathspecs
    (used by the rescue flow to stash only ``.litehive`` metadata).
    """
    args = ["stash", "push"]
    if include_untracked:
        args.append("-u")
    args.extend(["-m", message])
    if paths:
        args.append("--")
        args.extend(paths)
    proc = _run_git(cwd, *args)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def stash_apply(cwd: Path, ref: str) -> tuple[bool, str]:
    """Best-effort ``git stash apply <ref>``. Returns ``(success, message)``."""
    proc = _run_git(cwd, "stash", "apply", ref)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def stash_drop(cwd: Path, ref: str) -> None:
    """Best-effort ``git stash drop <ref>``. Silent on failure."""
    _run_git(cwd, "stash", "drop", ref)


def checkout_ours(cwd: Path, paths: list[str]) -> None:
    """Run ``git checkout --ours -- <paths>``. Best-effort.

    Used by the rescue flow when we want to keep our side of a
    merge conflict on a specific set of paths (typically task
    metadata) without bringing in the conflicting upstream copy.
    """
    if not paths:
        return
    _run_git(cwd, "checkout", "--ours", "--", *paths)


def restore_paths(
    cwd: Path,
    paths: list[str],
    source: str = "HEAD",
    staged: bool = True,
    worktree: bool = True,
) -> None:
    """Run ``git restore`` for the given paths.

    Wraps ``git restore --source=<source> [--staged] [--worktree]
    -- <paths>``. Used by the rescue flow to drop task-metadata
    changes from staging without disturbing other staged paths.
    """
    if not paths:
        return
    args = ["restore", f"--source={source}"]
    if staged:
        args.append("--staged")
    if worktree:
        args.append("--worktree")
    args.append("--")
    args.extend(paths)
    _run_git(cwd, *args)


def stash_pop(cwd: Path, ref: str | None = None, with_index: bool = False) -> tuple[bool, str]:
    """Run ``git stash pop [--index] [<ref>]``.

    Returns ``(success, stderr_or_stdout)``. ``with_index`` restores
    staging metadata; ``ref`` pops a specific stash entry rather than
    the top of the stack.
    """
    args = ["stash", "pop"]
    if with_index:
        args.append("--index")
    if ref is not None:
        args.append(ref)
    proc = _run_git(cwd, *args)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


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
    """Used by the daemon's origin-divergence check to classify local vs origin/main as fast-forward, behind, or genuinely diverged."""
    proc = _run_git(root, "merge-base", "--is-ancestor", ancestor_sha, descendant_sha)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError(proc.stderr.strip() or "git merge-base --is-ancestor failed")


def remove_worktree(root: Path, path: Path, force: bool = False) -> None:
    """Drop a managed worktree from the registry; the worktree-cleanup flow passes ``force=True`` because the directory may already be partially gone."""
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
    """Atomic cherry-pick: on conflict it auto-aborts so the workspace doesn't stay mid-pick, then re-raises so the caller treats the apply as failed."""
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
    """Stable subject string used to seed ``TaskRecord.git.commit_message`` so the rest of the system can detect "still on the generated message" via equality compare."""
    return DEFAULT_CHECKPOINT_SUBJECT_TEMPLATE.format(task_id=task_id, slug=slug)


def _clean_commit_text(value: str) -> str:
    """Normalize a stretch of user-authored task text for inclusion in a commit message body.

    Stripping trailing whitespace per line and the surrounding blank lines
    keeps the generated commit message stable across edits — important
    because ``_uses_generated_commit_message`` does an exact-string compare.
    """
    lines = [line.rstrip() for line in value.strip().splitlines()]
    return "\n".join(lines).strip()


def _metadata_body(task: TaskRecord) -> list[str]:
    """Build the trailer-style body lines that summarize ``task`` inside a generated commit message.

    Used by ``generated_completion_commit_message`` so completion commits
    carry a self-contained record of which task they implemented and what
    its acceptance criteria were, even if the original task is later deleted.
    """
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


def generated_completion_commit_message(task: TaskRecord, detail: str | None = None) -> str:
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
    """Append an "(attempt N)" suffix to the subject line of a commit message.

    Used by ``checkpoint_message`` and ``rollback_message`` so retries land
    as distinct commits with attempt-tagged subjects, making the recovery
    history obvious in ``git log --oneline``.
    """
    subject, separator, body = message.partition("\n")
    subject = CHECKPOINT_ATTEMPT_SUFFIX_TEMPLATE.format(base=subject, attempt=attempt)
    return subject + separator + body


def _uses_generated_commit_message(task: TaskRecord) -> bool:
    """Report whether the task is still riding the auto-generated commit message.

    Used by the commit-stage code to decide whether to overwrite
    ``task.git.commit_message`` with a freshly generated one when metadata
    changes — if the operator has hand-edited it we leave the override alone.
    """
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
    """Deterministic subject for rollback commits so :func:`find_commit_by_subject` can locate the right one when a task is rolled back twice."""
    return ROLLBACK_SUBJECT_TEMPLATE.format(task_id=task.id, slug=task.slug, attempt=attempt)


def find_commit_by_subject(root: Path, subject: str) -> str | None:
    """Locate a commit by exact subject match — the rollback path uses this to find the checkpoint commit it needs to revert without trusting in-memory state to still hold the sha."""
    proc = _run_git(root, "log", "--format=%H%x00%s")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git log failed")

    for line in proc.stdout.splitlines():
        sha, _, message = line.partition("\x00")
        if message == subject:
            return sha
    return None


def commit_task(root: Path, message: str, paths: list[str] | None = None) -> CommitCheckpoint | None:
    """Stage-and-commit helper for a task checkpoint: returns ``None`` (not an error) when there's nothing to commit so callers don't need to pre-check; ``paths`` scopes the add when only specific paths should land."""
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
    """Stage a ``git revert --no-commit`` of the task's last checkpoint; refuses to run if the worktree has user edits because reverting on top of dirty paths would entangle them with the rollback diff."""
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
    """Cancel an in-progress revert and clear out untracked-file conflicts that would otherwise leave the workspace stuck in MERGING; loops because removing one conflict path can expose the next one git complains about."""
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
