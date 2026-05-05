#!/usr/bin/env python3
"""Git wrapper used by the merge-resolver sandbox profile."""

import os
from pathlib import Path
import sys

from litehive.attention import append_attention_log
from litehive.workspace import Workspace

_PROTECTED_REFS = {"main", "master", "origin/main", "origin/master"}


def main(argv: list[str], real_git_path: str, workspace_root: str) -> int:
    """Entry point used as ``$LITEHIVE_REAL_GIT_PATH``'s shim: rejects destructive git invocations with an attention-log entry and exit code 2, otherwise ``execv``s the real git binary so the agent sees no shim at all; the merge-resolver sandbox profile points its ``PATH`` at this so an LLM-driven merge cannot ``push --force`` or rewrite history."""
    reason = rejection_reason(argv)
    if reason is not None:
        append_attention_log(
            Workspace.from_path(Path(workspace_root)),
            f"merge-resolver git wrapper rejected `{_format_cmd(argv)}`: {reason}",
        )
        print(f"litehive git wrapper: blocked destructive git command: {reason}", file=sys.stderr)
        return 2
    os.execv(real_git_path, [real_git_path, *argv])
    return 1


def rejection_reason(argv: list[str], cwd: Path | None = None) -> str | None:
    """Inspect a git argv and return a short reason string when the invocation matches one of the merge-resolver sandbox's denied patterns (force-push, history rewrite, hard reset to origin, rebase/cherry-pick onto protected refs, …) or None when it should be allowed; the single decision point that both :func:`main` and the unit tests use, factored out so test cases can assert the reason text without spawning subprocesses."""
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
    """Return positional args (filter out flags) so the rebase/cherry-pick checks can scan only ref-shaped arguments rather than colliding with ``--onto`` or ``-i``; called by :func:`rejection_reason` for those two commands."""
    return [arg for arg in argv if arg and not arg.startswith("-")]


def _is_origin_ref(value: str) -> bool:
    """Return True when an argv token looks like ``origin/<branch>``; used by :func:`rejection_reason` to block ``git reset --hard origin/main`` and friends."""
    return value.startswith("origin/")


def _is_protected_ref(value: str) -> bool:
    """Return True for refs the merge-resolver sandbox refuses to rebase/cherry-pick onto (main/master locally, anything under ``origin/``, anything under ``refs/remotes/``); the policy table that :func:`rejection_reason` consults for those commands."""
    return value in _PROTECTED_REFS or value.startswith("origin/") or value.startswith("refs/remotes/")


def _current_ref(cwd: Path) -> str | None:
    """Read ``HEAD`` directly from disk to determine the currently-checked-out branch, returning the short branch name (e.g. ``main``) or None for detached/unparseable HEADs; called by :func:`rejection_reason` so we can block ``rebase``/``cherry-pick`` when the sandbox is currently sitting on a protected ref."""
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
    """Walk up from ``cwd`` to find the closest ``.git`` directory, dereferencing the ``gitdir: …`` indirection that worktrees use so we can read HEAD inside a worktree just like inside the main repo; called by :func:`_current_ref`. Returns None when no git directory is found, which makes the rebase/cherry-pick checks no-op outside a repo (as ``execv`` would simply fail anyway)."""
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
    """Render argv as a single ``git foo bar`` string for the attention-log entry; cosmetic helper that exists so the log line shows the full rejected command, not just the rejection reason."""
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
