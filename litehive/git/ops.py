"""
Single owner module for every ``subprocess.run(["git", ...])`` in litehive.

Code-style "Side-Effecting Subsystems" requires every git invocation
to flow through here so sandboxing, error translation, and ``cwd``
discipline stay in one place. The wrappers fall into a few groups:
read-only probes (``is_git_repo``, ``current_head``, ``status_porcelain``,
``stdout_or_none``…), mutation helpers (``add_paths``,
``commit_with_message_stdin``, worktree add/remove, stash push/pop),
and the commit-message templating used by the runner. New helpers
land here rather than as ad-hoc subprocess calls in callers.
"""

import subprocess
from pathlib import Path

from litehive.domain.task import TaskRecord

DEFAULT_CHECKPOINT_SUBJECT_TEMPLATE = "litehive: complete {task_id} {slug}"
CHECKPOINT_ATTEMPT_SUFFIX_TEMPLATE = "{base} (attempt {attempt})"
COMPLETION_SUBJECT_TEMPLATE = "litehive {task_id}: {title}"


class GitError(RuntimeError):
    """
    Raised when a git operation fails in a way the caller must handle.

    Wrapping ``subprocess.CalledProcessError`` would leak transport
    details into domain code; wrapping ``Exception`` would over-catch.
    A dedicated class lets the worktree, rescue, and runner flows
    catch git-specific failures while leaving unrelated exceptions
    propagating normally.
    """


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """
    Run a git subprocess against ``root`` capturing stdout/stderr without raising.

    The shared bottom of every wrapper in this module. Each caller
    branches on ``returncode`` and ``stderr`` to translate git
    failures into domain-typed errors (``GitError``), so we never let
    ``CalledProcessError`` leak past the git layer — callers should
    catch ``GitError``, not the subprocess error class.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def is_git_repo(root: Path) -> bool:
    """
    Cheap probe used at every git-touching entry point.

    Lets worktree, status, and rescue code short-circuit when the
    workspace isn't a checkout yet (e.g. a brand-new workspace
    where the operator hasn't run ``git init``); without this, every
    helper would hit the same "fatal: not a git repository" error
    and we would have to translate it everywhere.
    """
    proc = _run_git(root, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def has_changes(root: Path) -> bool:
    """
    Quick "is the worktree dirty at all?" check.

    Used by the rebase helper before stashing so we can skip the
    stash dance on a clean tree. The faster signal vs.
    ``status_porcelain`` is the trade-off — this returns at the first
    dirty entry without listing them.
    """
    proc = _run_git(root, "status", "--porcelain")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def status_porcelain(root: Path) -> list[str]:
    """
    Full porcelain listing including all untracked files.

    Raises ``GitError`` rather than returning an empty list on
    failure so callers see the workspace truthfully — silently
    returning ``[]`` on a git error would let the dirty-worktree
    gate falsely conclude "nothing to clean up" and let a corrupt
    workspace through.
    """
    proc = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git status failed")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def has_non_litehive_changes(root: Path) -> bool:
    """
    True when the worktree has edits outside ``.litehive/`` metadata.

    The runner and rollback paths must not steamroll user-authored
    edits, but they do produce per-task ``.litehive/`` metadata
    churn that's safe to overwrite — this helper is the boundary
    between "real user work" and "our bookkeeping".
    """
    for line in status_porcelain(root):
        if len(line) > 3:
            path = line[3:]
        else:
            path = ""
        if path and not path.startswith(".litehive/"):
            return True
    return False


def current_head(root: Path) -> str | None:
    """
    Soft HEAD lookup returning ``None`` for an unborn repo.

    The strict counterpart :func:`head_sha_strict` raises instead.
    Use this when a missing HEAD is a "not initialized yet" signal
    (e.g. a freshly init'd workspace before the first commit) and
    the caller knows what to do; use the strict variant when a
    missing HEAD is a hard error.
    """
    proc = _run_git(root, "rev-parse", "--verify", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def rev_parse_verify(cwd: Path, ref: str) -> str | None:
    """
    Resolve a ref to a SHA via ``git rev-parse --verify <ref>``.

    Returns ``None`` when the ref does not resolve so callers can
    branch on absence without try/except. Used by the daemon's
    origin-divergence check to compare ``main`` against
    ``origin/main`` and by the rescue flow to record the post-stash
    stash ref.
    """
    proc = _run_git(cwd, "rev-parse", "--verify", ref)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def list_remote_names(cwd: Path) -> list[str]:
    """
    Return the configured remote names (``git remote``).

    Returns an empty list on failure (e.g. not a git repo) rather
    than raising so the daemon's origin-divergence check can do
    its "do we even have an origin?" probe with a single membership
    test instead of a try/except.
    """
    proc = _run_git(cwd, "remote")
    if proc.returncode != 0:
        return []
    return [name for name in proc.stdout.split() if name]


def fetch(cwd: Path, remote: str, *refs: str) -> tuple[bool, str]:
    """
    Best-effort ``git fetch <remote> <refs...>``.

    Returns ``(success, stderr)``. Network failures are intentionally
    not raised — the daemon's pool gate must not halt on a transient
    network error, only on real divergence; callers that need a
    failure-loud variant can branch on the success flag.
    """
    proc = _run_git(cwd, "fetch", remote, *refs)
    return proc.returncode == 0, proc.stderr.strip()


def delete_branch(cwd: Path, branch: str) -> None:
    """
    Best-effort ``git branch -D <branch>`` in ``cwd``.

    Used after a managed worktree is removed so the branch ref
    doesn't shadow a future task with the same slug. Ignores
    failures (e.g. branch already gone) — the cleanup call site
    doesn't act on the result, and surfacing the error would
    convert a successful cleanup into a confusing failure.
    """
    _run_git(cwd, "branch", "-D", branch)


def remote_url(cwd: Path, remote: str = "origin") -> str | None:
    """
    Return the URL configured for ``remote``, or ``None`` if missing.

    Wraps ``git remote get-url <remote>``. Used by the worktree
    sync flow to ask "does this checkout have an origin?" without
    raising a noisy ``GitError`` on the missing-remote path —
    local-only workspaces are a real configuration we silently
    skip the origin merge for.
    """
    proc = _run_git(cwd, "remote", "get-url", remote)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def head_sha_strict(cwd: Path) -> str:
    """
    Return the HEAD SHA, raising :class:`GitError` if it can't be read.

    The non-strict counterpart is :func:`current_head`. Use this
    when the caller treats a missing HEAD as a hard error (e.g.
    the runner about to write a commit needs to know the parent)
    rather than a "not initialized yet" signal.
    """
    proc = _run_git(cwd, "rev-parse", "HEAD")
    if proc.returncode != 0:
        raise GitError(f"cannot read HEAD at {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def current_branch(cwd: Path) -> str | None:
    """
    Return the current branch name, or ``None`` for a detached HEAD.

    Wraps ``git symbolic-ref --quiet --short HEAD``. Used by the
    runner to know which branch a worktree is on so it can pick
    the correct merge target; detached HEADs return ``None`` so
    callers can refuse the operation rather than guessing.
    """
    proc = _run_git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def cherry_pick_no_commit(cwd: Path, sha: str) -> tuple[bool, str]:
    """
    Run ``git cherry-pick --no-commit <sha>``.

    Returns ``(success, stderr_or_stdout)``. The worktree rescue
    flow uses ``--no-commit`` so it can drop task metadata from
    the staged result before the actual commit lands; a normal
    cherry-pick would commit the metadata before we got the
    chance to filter it.
    """
    proc = _run_git(cwd, "cherry-pick", "--no-commit", sha)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def cherry_pick_abort(cwd: Path) -> None:
    """
    Best-effort ``git cherry-pick --abort``.

    Silent on failure because the call sites only invoke this on
    the failure path of an already-failed cherry-pick — we have
    nothing left to recover and surfacing an abort error would
    just mask the original failure.
    """
    _run_git(cwd, "cherry-pick", "--abort")


def index_has_staged_changes(cwd: Path) -> bool:
    """
    True when the index has staged changes ready to commit.

    Wraps ``git diff --cached --quiet --exit-code`` and translates
    git's exit codes: 0 = no diff, 1 = differs, anything else =
    real error (raises :class:`GitError`). Used by the rescue
    flow to skip the commit step when metadata stripping left an
    empty index.
    """
    proc = _run_git(cwd, "diff", "--cached", "--quiet", "--exit-code")
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    raise GitError(f"git diff --cached failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")


def commit_reuse_message(cwd: Path, sha: str) -> tuple[bool, str]:
    """
    Run ``git commit --reuse-message=<sha>``.

    Returns ``(success, stderr_or_stdout)``. The rescue flow uses
    this so the rescued commit keeps the original commit's
    author/date/message metadata — a fresh commit message would
    erase the audit trail of which task originally produced the
    work.
    """
    proc = _run_git(cwd, "commit", "--reuse-message", sha)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def cherry_check(cwd: Path, upstream_sha: str, head_sha: str) -> list[str] | None:
    """
    Run ``git cherry <upstream> <head>`` and return the marker lines.

    Returns ``None`` when the call fails. On success each entry is
    ``+ <sha>`` (commit needs to land) or ``- <sha>`` (already in
    upstream). The worktree-rescue flow asks "are this branch's
    commits already on main?" — answering by patch id rather than
    sha catches manually rebased history that ``git log --left-right``
    would falsely call divergent.
    """
    proc = _run_git(cwd, "cherry", upstream_sha, head_sha)
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def status_porcelain_with_options(cwd: Path, include_ignored: bool = False) -> list[str]:
    """
    ``git status --porcelain`` with optional ``--ignored --untracked-files=all``.

    The runner's auto-commit code uses this to enumerate dirty
    entries before staging. ``include_ignored=True`` is for the
    code paths that need to see ignored files too (recovery
    diagnostics); the default omits them so normal status calls
    don't flood with build-artifact noise.
    """
    args = ["status", "--porcelain"]
    if include_ignored:
        args.extend(["--ignored", "--untracked-files=all"])
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        raise GitError(f"git status failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def add_paths(cwd: Path, paths: list[str], all_flag: bool = False) -> None:
    """
    Run ``git add [--all] -- <paths>`` in ``cwd``.

    Raises :class:`GitError` on failure so the surrounding commit
    flow halts before producing an empty or partial commit.
    ``all_flag=True`` is used for cleanup commits where the runner
    wants new untracked files included; the default omits ``--all``
    so callers stage only the paths they explicitly named.
    """
    args = ["add"]
    if all_flag:
        args.append("--all")
    args.extend(["--", *paths])
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        raise GitError(f"git add failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")


def commit_with_message_stdin(cwd: Path, message: str) -> None:
    """
    Commit currently-staged changes with ``message`` piped via stdin.

    Wraps ``git commit -F -`` so the runner can pass multi-line
    commit bodies (with newlines, quotes, and shell metacharacters)
    without escaping them onto the command line. Raises
    ``GitError`` on commit failure so the surrounding lifecycle
    transition fails loudly instead of leaving a half-committed
    state.
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
    """
    Conclude an in-progress merge with ``git commit --no-edit``.

    Used after the merge-resolver lifecycle node finishes resolving
    conflicts: the index is already in the desired shape, the
    operator has reviewed it, and the commit just needs to land
    with the auto-generated merge message. Raises ``GitError`` on
    failure so a wedged merge surfaces instead of being silently
    skipped.
    """
    proc = _run_git(cwd, "commit", "--no-edit")
    if proc.returncode != 0:
        raise GitError(
            f"git commit --no-edit failed in {cwd}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def is_path_tracked(cwd: Path, path: str) -> bool:
    """
    Whether ``path`` is currently tracked in the index.

    Used to filter the path list before a ``git add``: a vanished
    path that's still tracked is safe to add (git will stage it
    as a deletion), but a vanished path that was never tracked
    would error out the whole batch. The pre-filter keeps that
    error from cascading.
    """
    return _run_git(cwd, "ls-files", "--error-unmatch", "--", path).returncode == 0


def check_ignore(cwd: Path, path: str) -> bool:
    """
    Whether ``path`` is ignored under the workspace's ``.gitignore`` rules.

    Wraps ``git check-ignore --quiet --no-index --``. ``--no-index``
    asks git to evaluate ignore rules for tracked paths too, which
    is what the runner wants when filtering old task-report
    artifacts out of a fresh ``git add``. Raises ``GitError`` for
    unexpected exit codes so a corrupt gitignore surfaces instead
    of being silently treated as "not ignored".
    """
    proc = _run_git(cwd, "check-ignore", "--quiet", "--no-index", "--", path)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError(f"git check-ignore failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")


def stdout_or_none(cwd: Path, *args: str) -> str | None:
    """
    Run ``git <args>`` and return stripped stdout, ``None`` on failure or empty.

    Used by worktree helpers that ask git read-only questions
    where "the call failed" and "the answer is empty" should
    both collapse to ``None`` for the caller's convenience (e.g.
    ``git rev-parse``, ``git branch --show-current``,
    ``git merge-base``). Avoids forcing every read-only call site
    to write a try/except block.
    """
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def stdout_lines(cwd: Path, *args: str) -> list[str]:
    """
    Run ``git <args>`` and return non-empty stripped output lines.

    Returns ``[]`` on failure or empty output. Used by worktree
    helpers that ask for path or revision lists
    (``git diff --name-only``, ``git rev-list``) — a list-shaped
    answer is what the caller actually wants, and an empty list
    on failure plays nicely with iteration.
    """
    value = stdout_or_none(cwd, *args)
    if value is None:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def path_differs_at_ref(cwd: Path, ref: str, path: str) -> bool:
    """
    Whether ``path`` differs between the worktree and ``ref``.

    Wraps ``git diff --quiet <ref> -- <path>`` and translates the
    exit codes: 0 = identical, 1 = differs, anything else =
    raise. Used by the recovery scope-analysis flow to ask "did
    this file change since branching off main?" without paying
    the cost of a full diff payload.
    """
    proc = _run_git(cwd, "diff", "--quiet", ref, "--", path)
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    raise GitError(proc.stderr.strip() or f"git diff --quiet failed for {path}")


def add_worktree(root: Path, path: Path, ref: str = "HEAD") -> None:
    """
    Create a detached-HEAD worktree at ``path`` for the runner's worktree pool.

    ``--detach`` is deliberate: the runner's experimental worktrees
    must not move the parent branch when an agent commits. The
    branch-named worktrees used for actual task work go through
    :func:`add_worktree_branch` instead.
    """
    proc = _run_git(root, "worktree", "add", "--detach", str(path), ref)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree add failed")


def prune_worktrees(root: Path, expire_now: bool = False) -> None:
    """
    Run ``git worktree prune``, optionally with ``--expire now``.

    ``expire_now=True`` forces git to garbage-collect stale
    worktree bookkeeping immediately rather than after the
    configured grace period. The runner uses it before re-adding
    a worktree at the same path so git doesn't refuse the add
    over a leftover registration for the now-deleted directory.
    """
    args = ["worktree", "prune"]
    if expire_now:
        args.extend(["--expire", "now"])
    proc = _run_git(root, *args)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree prune failed")


def add_worktree_branch(root: Path, branch: str, path: Path, ref: str = "HEAD", force: bool = False) -> None:
    """
    Create a new worktree for ``branch`` at ``path``.

    Wraps ``git worktree add [-f] -B <branch> <path> <ref>``. ``-B``
    creates or resets the branch so reused task ids don't fail
    when the previous run's branch still exists. ``--force`` lets
    the call succeed when ``path`` is already in git's worktree
    list — the runner cleaned up the directory but git still
    remembers it.
    """
    args = ["worktree", "add"]
    if force:
        args.append("--force")
    args.extend(["-B", branch, str(path), ref])
    proc = _run_git(root, *args)
    if proc.returncode != 0:
        raise GitError(f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}")


def list_worktrees_porcelain(root: Path) -> str:
    """
    Return the raw ``git worktree list --porcelain`` output.

    Returns the multi-block porcelain format unchanged so the
    caller (currently ``WorktreeService.registered_worktree_for_branch``)
    can parse it line-by-line; wrapping it in a structured return
    here would force every caller to understand whatever shape
    this function chose.
    """
    proc = _run_git(root, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        raise GitError(f"git worktree list failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def diff_name_status(cwd: Path, *args: str) -> list[tuple[str, str]]:
    """
    Return ``[(status, path), ...]`` for ``git diff --name-status <args>``.

    ``status`` is the single-letter diff code (``M``, ``A``,
    ``D``, ``R``, …). Used by the recovery scope-analysis flow
    to enumerate files added or deleted between branches without
    parsing a full diff payload — the recovery agent only needs
    the path and direction, not the patch content.
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
    """
    Whether ``ref:path`` resolves to an object.

    Wraps ``git cat-file -e <ref>:<path>``. The recovery
    scope-analysis flow uses this to ask "did this file exist on
    main when the worktree branched off?" — the answer drives
    whether changes count as additions or modifications.
    """
    proc = _run_git(cwd, "cat-file", "-e", f"{ref}:{path}")
    return proc.returncode == 0


def show_at_ref(cwd: Path, ref: str, path: str) -> str:
    """
    Return the contents of ``path`` at ``ref`` (``git show ref:path``).

    Raises :class:`GitError` when the path isn't present in the ref
    so callers can branch on the absence rather than silently
    pretending the file was empty. Used by recovery diagnostics
    that need the original file contents to explain a regression.
    """
    proc = _run_git(cwd, "show", f"{ref}:{path}")
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git show {ref}:{path} failed")
    return proc.stdout


def checkout_ref(cwd: Path, ref: str) -> bool:
    """
    Best-effort ``git checkout <ref>``.

    Used by the recovery scope-analysis flow to temporarily switch
    branches for a check, then switch back. Caller is expected to
    handle the ``False`` return because a failed checkout could
    mean an unmergeable state — recovery has its own opinion about
    what to do next.
    """
    return _run_git(cwd, "checkout", ref).returncode == 0


def stash_push(
    cwd: Path,
    message: str,
    include_untracked: bool = False,
    paths: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Run ``git stash push -m <message>``, optionally with ``-u`` and pathspecs.

    Returns ``(success, stderr_or_stdout)``. ``include_untracked``
    is needed for the rescue flow because new files would
    otherwise be left behind by the stash; ``paths`` scopes the
    stash to a specific subtree (rescue stashes only the
    ``.litehive`` metadata, not the operator's other edits).
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
    """
    Best-effort ``git stash apply <ref>``.

    Returns ``(success, message)`` so callers can distinguish a
    successful apply from a conflict. The rescue flow uses this
    as a fallback when ``stash pop`` refuses to drop on conflict.
    """
    proc = _run_git(cwd, "stash", "apply", ref)
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def stash_drop(cwd: Path, ref: str) -> None:
    """
    Best-effort ``git stash drop <ref>``.

    Silent on failure because the call sites use this to clean up
    after a successful ``stash apply`` and a failed drop only
    leaves a (recoverable) reflog entry — surfacing the error
    would convert a successful rescue into a confusing failure.
    """
    _run_git(cwd, "stash", "drop", ref)


def checkout_ours(cwd: Path, paths: list[str]) -> None:
    """
    Run ``git checkout --ours -- <paths>``, best-effort.

    Used by the rescue flow to keep our side of a merge conflict
    on task-metadata paths without bringing in the conflicting
    upstream copy. Empty path list is a no-op so callers can
    invoke unconditionally.
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
    """
    Run ``git restore --source=... [--staged] [--worktree] -- <paths>``.

    The rescue flow uses this to drop task-metadata changes from
    the staged cherry-pick without disturbing other staged paths
    — passing both ``--staged`` and ``--worktree`` ensures the
    file matches HEAD on disk too, so the next status doesn't
    show the file as still dirty.
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
    """
    Run ``git stash pop [--index] [<ref>]``.

    Returns ``(success, stderr_or_stdout)``. ``with_index=True``
    restores the original staging metadata so a stashed commit
    boundary survives the pop; ``ref=None`` pops the top of the
    stash stack but every litehive caller passes an explicit ref
    to avoid racing concurrent stashes.
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
    """
    Run ``git merge <ref> --no-edit`` in ``cwd``.

    Returns ``(success, stderr_or_stdout)``. The merge-resolver
    flow needs to distinguish a clean merge from a conflict and
    surface the message either way; ``--no-edit`` skips the
    interactive editor that would block a daemon child.
    """
    proc = _run_git(cwd, "merge", ref, "--no-edit")
    if proc.returncode == 0:
        return True, proc.stdout
    return False, proc.stderr.strip() or proc.stdout.strip()


def merge_abort(cwd: Path) -> None:
    """
    Best-effort ``git merge --abort`` in ``cwd``.

    Used after a conflicting merge couldn't be resolved (either
    automatically or by the merge-resolver agent) so the worktree
    isn't left in a half-merged state for the next sync to trip
    over. Failure is intentionally silent — we're already on the
    failure path and have nothing more to recover.
    """
    _run_git(cwd, "merge", "--abort")


def unmerged_files(cwd: Path) -> list[str]:
    """
    Return paths with merge conflicts (``--diff-filter=U``).

    Called after ``merge_no_edit`` returns a non-success result to
    enumerate the files the merge-resolver agent needs to fix.
    The agent's prompt context lists these paths verbatim so it
    knows exactly which conflicts to resolve.
    """
    proc = _run_git(cwd, "diff", "--name-only", "--diff-filter=U")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def is_ancestor(root: Path, ancestor_sha: str, descendant_sha: str) -> bool:
    """
    Whether ``ancestor_sha`` is an ancestor of ``descendant_sha``.

    The daemon's origin-divergence check uses this to classify the
    relationship between ``main`` and ``origin/main``: equal/either-
    side ancestor = fast-forward (safe), neither ancestor = real
    divergence (halt). Wraps ``git merge-base --is-ancestor`` so
    the boolean answer skips the cost of computing the actual
    merge base.
    """
    proc = _run_git(root, "merge-base", "--is-ancestor", ancestor_sha, descendant_sha)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError(proc.stderr.strip() or "git merge-base --is-ancestor failed")


def remove_worktree(root: Path, path: Path, force: bool = False) -> None:
    """
    Drop a managed worktree from git's registry.

    The worktree-cleanup flow always passes ``force=True`` because
    the directory may already be partially gone (the cleanup
    helper deletes the tree first and then asks git to forget
    it). Without ``--force`` git would refuse on any of those
    states.
    """
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    proc = _run_git(root, *args)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or "git worktree remove failed")


def rebase_worktree_onto(worktree: Path, target_ref: str) -> bool:
    """
    Rebase the worktree's HEAD onto ``target_ref``, stashing dirty edits.

    Returns ``True`` on success, ``False`` if the rebase
    conflicted — in the false case the worktree is left
    unchanged (rebase aborted, stash popped) so callers can
    decide whether to escalate to merge-resolver or report the
    failure. Used by ``WorktreeService`` to keep task worktrees
    on top of fresh ``main`` before each pre-exec.
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


def default_commit_message(task_id: str, slug: str) -> str:
    """
    Stable subject string used to seed ``TaskRecord.git.commit_message``.

    The exact-string equality check in :func:`_uses_generated_commit_message`
    decides whether the operator has hand-edited the message; if
    the generated subject ever drifted across calls for the same
    task, that detection would falsely conclude every task had
    been edited.
    """
    return DEFAULT_CHECKPOINT_SUBJECT_TEMPLATE.format(task_id=task_id, slug=slug)


def _clean_commit_text(value: str) -> str:
    """
    Normalize user-authored task text for inclusion in a commit body.

    Stripping trailing whitespace per line and surrounding blank
    lines keeps the generated commit message stable across edits
    — load-bearing because :func:`_uses_generated_commit_message`
    does an exact-string compare to decide whether the operator
    has overridden the message.
    """
    stripped = value.strip()
    raw_lines = stripped.splitlines()
    cleaned: list[str] = []
    for line in raw_lines:
        cleaned.append(line.rstrip())
    return "\n".join(cleaned).strip()


def _metadata_body(task: TaskRecord) -> list[str]:
    """
    Build the trailer-style body lines summarizing ``task`` for a commit message.

    Used by :func:`generated_completion_commit_message` so the
    commit carries a self-contained record of which task it
    implemented and what its acceptance criteria were — important
    because the task record itself may be deleted later, but
    ``git log`` will preserve the body forever.
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
        lines.extend(_acceptance_criteria_bullets(task.acceptance_criteria))
    return lines


def _acceptance_criteria_bullets(criteria: list[str]) -> list[str]:
    """
    Format task acceptance criteria as commit-body bullet lines.

    Each criterion runs through :func:`_clean_commit_text` to
    normalize whitespace, and any criterion that goes empty after
    cleaning is dropped so the commit body never has a stray
    ``"- "`` placeholder. Caller: :func:`_metadata_body`.
    """
    bullets: list[str] = []
    for item in criteria:
        cleaned = _clean_commit_text(item)
        if cleaned:
            bullets.append(f"- {cleaned}")
    return bullets


def generated_completion_commit_message(task: TaskRecord, detail: str | None = None) -> str:
    """
    Return litehive's generated completion commit message for a task.

    Composes a compact subject (so ``git log --oneline`` stays
    readable) with a body carrying the persisted task metadata
    (id, title, goal, acceptance criteria) so the commit explains
    what was completed even after the task record is gone.
    Optional ``detail`` adds a final commit-detail trailer.
    """
    subject = COMPLETION_SUBJECT_TEMPLATE.format(task_id=task.id, title=task.title)
    lines = [subject, "", *_metadata_body(task)]
    if detail:
        lines.extend(["", f"Commit detail: {detail}"])
    return "\n".join(lines)


def _with_attempt_suffix(message: str, attempt: int) -> str:
    """
    Append an ``(attempt N)`` suffix to the subject line of a commit message.

    Used by :func:`checkpoint_message` so retries land as distinct
    commits with attempt-tagged subjects, making recovery history
    obvious in ``git log --oneline``. Splits subject from body so
    only the subject gets the suffix; the body keeps its original
    metadata trailers intact.
    """
    subject, separator, body = message.partition("\n")
    subject = CHECKPOINT_ATTEMPT_SUFFIX_TEMPLATE.format(base=subject, attempt=attempt)
    return subject + separator + body


def _uses_generated_commit_message(task: TaskRecord) -> bool:
    """
    True when the task's commit message hasn't been hand-edited.

    The commit-stage code consults this before regenerating the
    commit message from updated metadata — operators who set a
    custom subject (via ``litehive update --commit-message``)
    expect their override to stick across stage transitions, but
    auto-generated messages should track the latest task metadata.
    """
    message = task.git.commit_message
    if message is None:
        return True
    return message == default_commit_message(task.id, task.slug)


def checkpoint_message(task: TaskRecord, attempt: int | None = None) -> str:
    """
    Return the deterministic checkpoint commit message for an attempt.

    Used by the commit stage when landing a checkpoint commit;
    ``attempt=None`` picks the next sequential attempt from the
    task's ``checkpoint_attempts`` counter. Hand-edited messages
    pass through unchanged (no ``(attempt N)`` suffix) because the
    operator owns those — the suffix would silently rewrite
    operator copy across retries.
    """
    if _uses_generated_commit_message(task):
        base = generated_completion_commit_message(task)
    else:
        base = task.git.commit_message or default_commit_message(task.id, task.slug)
    attempt = attempt or (task.git.checkpoint_attempts + 1)
    if attempt > 1 and _uses_generated_commit_message(task):
        return _with_attempt_suffix(base, attempt)
    return base


