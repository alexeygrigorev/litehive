#!/usr/bin/env python3
"""
Sandboxed git shim for the merge-resolver subagent.

The merge-resolver sandbox profile points the agent's ``PATH`` at this
shim so an LLM-driven merge cannot ``push --force``, rewrite history,
or reset against ``origin/*``. ``rejection_reason`` is the single
decision point — it returns a short reason string for blocked commands
and ``None`` for allowed ones; ``main`` calls it from the entry point
and ``execv``s the real git binary on the allowed path.
"""

import os
from pathlib import Path
import sys

from litehive.attention import AttentionRepository
from litehive.container import build_workspace

_PROTECTED_REFS = {"main", "master", "origin/main", "origin/master"}


def main(argv: list[str], real_git_path: str, workspace_root: str) -> int:
    """
    Sandbox entry point used as the merge-resolver agent's ``git``.

    Refuses destructive invocations with an attention-log entry and
    exit code 2 — the merge-resolver agent sees a non-zero exit and
    a short stderr reason just like a normal git failure. Allowed
    invocations are ``execv``'d to the real git binary so the agent
    sees no shim at all.
    """
    reason = rejection_reason(argv)
    if reason is None:
        os.execv(real_git_path, [real_git_path, *argv])
        return 1
    attention_repository = AttentionRepository(build_workspace(Path(workspace_root)))
    attention_repository.append(f"merge-resolver git wrapper rejected `{_format_cmd(argv)}`: {reason}")
    print(f"litehive git wrapper: blocked destructive git command: {reason}", file=sys.stderr)
    return 2


def rejection_reason(argv: list[str], cwd: Path | None = None) -> str | None:
    """
    Inspect a git argv and return the reason it should be blocked, or ``None``.

    The single decision point both :func:`main` and the unit tests
    consult so test cases can assert the reason text without
    spawning subprocesses. Blocks force-push, history rewrites
    (``filter-repo``/``filter-branch``/``reflog expire``/``gc --prune=now``),
    hard reset to ``origin/*``, ``remote set-url origin``, and
    ``rebase``/``cherry-pick`` onto protected refs — the patterns
    an LLM could plausibly stumble into that would cost real work.
    """
    if not argv:
        return None
    command = argv[0]
    tail = argv[1:]
    if command == "push" and any(arg in {"--force", "-f", "--force-with-lease", "--mirror"} for arg in tail):
        return "push with force or mirror is not allowed"
    if command in {"filter-repo", "filter-branch"}:
        return f"`git {command}` is not allowed"
    if command == "reflog" and tail[:1] == ["expire"]:
        return "`git reflog expire` is not allowed"
    if command == "gc" and any(arg == "--prune=now" or arg.startswith("--prune=now") for arg in tail):
        return "`git gc --prune=now` is not allowed"
    if command == "update-ref" and "-d" in tail:
        for arg in tail:
            if arg.startswith("refs/remotes/"):
                return "deleting remote refs via `git update-ref -d` is not allowed"
    if command == "reset" and "--hard" in tail and any(_is_origin_ref(arg) for arg in tail):
        return "`git reset --hard` against origin/* is not allowed"
    if command == "remote" and len(tail) >= 2 and tail[0] == "set-url" and tail[1] == "origin":
        return "`git remote set-url origin` is not allowed"
    current_ref = _current_ref(cwd or Path.cwd())
    if command == "rebase":
        if current_ref is not None and _is_protected_ref(current_ref):
            return "`git rebase` while on a protected ref is not allowed"
        if any(_is_protected_ref(arg) for arg in _non_option_args(tail)):
            return "`git rebase` onto a protected ref is not allowed"
    if command == "cherry-pick":
        if current_ref is not None and _is_protected_ref(current_ref):
            return "`git cherry-pick` while on a protected ref is not allowed"
        if any(_is_protected_ref(arg) for arg in _non_option_args(tail)):
            return "`git cherry-pick` onto a protected ref is not allowed"
    return None


def _non_option_args(argv: list[str]) -> list[str]:
    """
    Return positional args, dropping flags so ref checks don't catch ``--onto``.

    The rebase/cherry-pick blocks scan for protected refs; without
    filtering, ``-i`` / ``--onto`` / ``--strategy`` would all
    falsely match the ``starts with "-"`` token check. Called by
    :func:`rejection_reason` for those two commands.
    """
    return [arg for arg in argv if arg and not arg.startswith("-")]


def _is_origin_ref(value: str) -> bool:
    """
    True when an argv token looks like ``origin/<branch>``.

    Used by :func:`rejection_reason` to catch ``git reset --hard
    origin/main`` and friends — the destructive part is the hard
    reset against a remote-tracking ref, which would silently
    drop unpushed work.
    """
    return value.startswith("origin/")


def _is_protected_ref(value: str) -> bool:
    """
    True for refs the sandbox refuses to rebase or cherry-pick onto.

    Policy table consulted by :func:`rejection_reason`: ``main`` /
    ``master`` locally, anything under ``origin/``, and anything
    under ``refs/remotes/``. These are the refs where a rewritten
    history would corrupt the operator's source of truth.
    """
    return value in _PROTECTED_REFS or value.startswith("origin/") or value.startswith("refs/remotes/")


def _current_ref(cwd: Path) -> str | None:
    """
    Read ``HEAD`` directly off disk to determine the current branch.

    Returns the short branch name (e.g. ``main``) or ``None`` for
    detached / unparseable HEADs. Called by :func:`rejection_reason`
    so we can block ``rebase`` / ``cherry-pick`` when the sandbox
    is currently sitting on a protected ref — without that, an
    agent could rebase main onto a topic branch.
    """
    git_dir = _resolve_git_dir(cwd)
    if git_dir is None:
        return None
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return None
    ref = head[5:]
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    if ref.startswith("refs/remotes/"):
        return ref.removeprefix("refs/remotes/")
    return ref


def _resolve_git_dir(cwd: Path) -> Path | None:
    """
    Walk up from ``cwd`` to find the git directory, dereferencing worktree pointers.

    Worktrees store ``HEAD`` in a sibling ``gitdir: <path>``
    indirection rather than in the worktree's own ``.git``
    directory; following the indirection lets :func:`_current_ref`
    read HEAD inside a worktree just like inside the main repo.
    Returns ``None`` outside a repo so the rebase/cherry-pick
    checks become a no-op — ``execv`` would fail anyway and we
    don't want to invent a refusal for a non-git scenario.
    """
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        git_entry = candidate / ".git"
        if git_entry.is_dir():
            return git_entry
        if git_entry.is_file():
            try:
                raw = git_entry.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            prefix = "gitdir: "
            if raw.startswith(prefix):
                return (git_entry.parent / raw[len(prefix) :]).resolve()
            return None
    return None


def _format_cmd(argv: list[str]) -> str:
    """
    Render argv as a single ``git foo bar`` string for the attention log.

    The attention-log entry shows both the full rejected command
    and the reason — without the command an operator reading the
    log later would see "merge-resolver git wrapper rejected ...:
    push with force or mirror is not allowed" with no idea which
    push was blocked.
    """
    if not argv:
        return "git"
    return "git " + " ".join(argv)


if __name__ == "__main__":
    raise SystemExit(
        main(
            sys.argv[1:],
            real_git_path=os.environ["LITEHIVE_REAL_GIT_PATH"],
            workspace_root=os.environ["LITEHIVE_WORKSPACE_ROOT"],
        )
    )
