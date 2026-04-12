"""Git wrapper used by the merge-resolver sandbox profile."""

import os
from pathlib import Path
import sys

from litehive.attention import record_attention

_PROTECTED_REFS = {"main", "master", "origin/main", "origin/master"}


def main(argv: list[str], *, real_git_path: str, workspace_root: str) -> int:
    reason = rejection_reason(argv)
    if reason is not None:
        record_attention(
            Path(workspace_root),
            kind="destructive_git_denied",
            title="Destructive git command was blocked",
            reason=f"`{_format_cmd(argv)}` was rejected: {reason}",
            suggested_action=(
                "Use a non-destructive git recovery path instead. Once reviewed,"
                " clear the queue item with `litehive attention resolve <id>`."
            ),
            dedupe_key=f"destructive_git_denied:{_format_cmd(argv)}:{reason}",
            metadata={"command": _format_cmd(argv), "rejection_reason": reason},
            log_message=f"merge-resolver git wrapper rejected `{_format_cmd(argv)}`: {reason}",
        )
        print(f"litehive git wrapper: blocked destructive git command: {reason}", file=sys.stderr)
        return 2
    os.execv(real_git_path, [real_git_path, *argv])
    return 1


def rejection_reason(argv: list[str], *, cwd: Path | None = None) -> str | None:
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
    return [arg for arg in argv if arg and not arg.startswith("-")]


def _is_origin_ref(value: str) -> bool:
    return value.startswith("origin/")


def _is_protected_ref(value: str) -> bool:
    return value in _PROTECTED_REFS or value.startswith("origin/") or value.startswith("refs/remotes/")


def _current_ref(cwd: Path) -> str | None:
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
    return "git" if not argv else "git " + " ".join(argv)
