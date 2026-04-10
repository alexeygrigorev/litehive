"""Inspect and clean Litehive-managed task worktrees."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import subprocess

from litehive.config import ensure_workspace
from litehive.git import (
    GitError,
    current_head,
    has_non_litehive_changes,
    remove_worktree,
    status_porcelain,
)
from litehive.tasks.crud import (
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    list_tasks,
    save_task,
    set_task_commit_sha,
)
from litehive.tasks.journal import append_journal
from litehive.tasks.persistence import load_state, save_state

_CLEANABLE_STATUSES = {"done", "deferred", "wont_do", "duplicate"}


@dataclass(slots=True)
class _ManagedWorktree:
    task_id: str
    status: str
    worktree_rel: str
    worktree_path: Path
    change_count: int
    active: bool

    @property
    def cleanable(self) -> bool:
        return self.status in _CLEANABLE_STATUSES and not self.active


@dataclass(slots=True)
class _RescueCandidate:
    task_id: str
    worktree_rel: str
    worktree_path: Path
    commit_shas: list[str]


@dataclass(slots=True)
class _RescueResult:
    task_id: str
    worktree_rel: str
    status: str
    commit_shas: list[str]
    head_sha: str | None = None
    message: str | None = None


def _cmd_worktree_ls(args):
    ensure_workspace(args.workspace)
    worktrees = _collect_managed_worktrees(args.workspace)
    print(f"workspace: {args.workspace}")
    print(f"worktree_count: {len(worktrees)}")
    if not worktrees:
        print("worktrees: none")
        return 0
    for item in worktrees:
        print()
        print(f"task_id: {item.task_id}")
        print(f"status: {item.status}")
        print(f"change_count: {item.change_count}")
        print(f"worktree_path: {item.worktree_rel}")
        print(f"active: {'yes' if item.active else 'no'}")
    return 0


def _cmd_worktree_clean(args):
    ensure_workspace(args.workspace)
    worktrees = _collect_managed_worktrees(args.workspace)
    candidates = [item for item in worktrees if item.cleanable]
    skipped_active = [item for item in worktrees if item.active]

    print(f"workspace: {args.workspace}")
    print(f"dry_run: {'yes' if args.dry_run else 'no'}")
    for item in candidates:
        print(f"would_remove: {item.task_id} {item.status} {item.worktree_rel}")
    for item in skipped_active:
        print(f"skipped_active: {item.task_id} {item.status} {item.worktree_rel}")

    if args.dry_run:
        print(f"removed_count: 0")
        print(f"would_remove_count: {len(candidates)}")
        return 0

    failures: list[tuple[_ManagedWorktree, str]] = []
    removed: list[_ManagedWorktree] = []
    removed_count = 0
    for item in candidates:
        try:
            remove_worktree(args.workspace, item.worktree_path, force=True)
            task = get_task(args.workspace, item.task_id)
            if task is not None:
                clear_task_worktree_path(task)
                save_task(args.workspace, task)
            removed.append(item)
            removed_count += 1
        except GitError as exc:
            failures.append((item, str(exc)))

    for item in removed:
        print(f"removed: {item.task_id} {item.status} {item.worktree_rel}")
    for item, message in failures:
        print(f"remove_failed: {item.task_id} {message}")
    print(f"removed_count: {removed_count}")
    return 1 if failures else 0


def _cmd_worktree_rescue(args):
    ensure_workspace(args.workspace)
    candidates = _collect_rescue_candidates(args.workspace)

    print(f"workspace: {args.workspace}")
    print(f"candidate_count: {len(candidates)}")
    if not candidates:
        print("rescues: none")
        return 0

    if not args.apply:
        for candidate in candidates:
            print()
            print(f"task_id: {candidate.task_id}")
            print(f"worktree_path: {candidate.worktree_rel}")
            if candidate.commit_shas:
                print(f"commit_count: {len(candidate.commit_shas)}")
                print("commits:")
                for sha in candidate.commit_shas:
                    print(f"  - {sha}")
            else:
                print("commit_count: 0")
                print("commits: (none)")
        return 0

    try:
        _require_clean_main_checkout(args.workspace)
    except GitError as exc:
        print(f"apply_error: {exc}")
        return 1

    results = [_apply_rescue_candidate(args.workspace, candidate) for candidate in candidates]
    clean_count = sum(1 for item in results if item.status == "clean")
    already_landed_count = sum(1 for item in results if item.status == "already_landed")
    manual_conflict_count = sum(1 for item in results if item.status == "manual_conflict")
    missing_worktree_count = sum(1 for item in results if item.status == "missing_worktree")
    no_commits_count = sum(1 for item in results if item.status == "no_commits")

    for item in results:
        print()
        print(f"task_id: {item.task_id}")
        print(f"worktree_path: {item.worktree_rel}")
        print(f"status: {item.status}")
        if item.commit_shas:
            print(f"commit_count: {len(item.commit_shas)}")
        if item.head_sha:
            print(f"head_sha: {item.head_sha}")
        if item.message:
            print(f"message: {item.message}")

    print()
    print(f"clean_count: {clean_count}")
    print(f"already_landed_count: {already_landed_count}")
    print(f"manual_conflict_count: {manual_conflict_count}")
    print(f"missing_worktree_count: {missing_worktree_count}")
    print(f"no_commits_count: {no_commits_count}")
    return 1 if manual_conflict_count or missing_worktree_count else 0


def _collect_managed_worktrees(root: Path) -> list[_ManagedWorktree]:
    state = load_state(root)
    active_task = get_task(root, state.active_task_id) if state.active_task_id else None
    active_path = get_task_worktree_path(active_task) if active_task is not None else None

    worktrees: list[_ManagedWorktree] = []
    for task in list_tasks(root):
        worktree_rel = get_task_worktree_path(task)
        if not _is_litehive_managed_worktree(worktree_rel):
            continue
        worktree_path = (root / worktree_rel).resolve()
        if not worktree_path.exists():
            continue
        try:
            change_count = len(status_porcelain(worktree_path))
        except GitError:
            change_count = 0
        worktrees.append(
            _ManagedWorktree(
                task_id=task.id,
                status=task.status,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                change_count=change_count,
                active=task.id == state.active_task_id or worktree_rel == active_path,
            )
        )
    return sorted(worktrees, key=lambda item: item.task_id)


def _collect_rescue_candidates(root: Path) -> list[_RescueCandidate]:
    candidates: list[_RescueCandidate] = []
    for task in list_tasks(root):
        if task.status != "merge_failed":
            continue
        worktree_rel = get_task_worktree_path(task)
        if not _is_litehive_managed_worktree(worktree_rel):
            continue
        worktree_path = (root / worktree_rel).resolve()
        commit_shas = _worktree_commits_ahead_of_main(root, worktree_path) if worktree_path.exists() else []
        candidates.append(
            _RescueCandidate(
                task_id=task.id,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                commit_shas=commit_shas,
            )
        )
    return sorted(candidates, key=lambda item: item.task_id)


def _apply_rescue_candidate(root: Path, candidate: _RescueCandidate) -> _RescueResult:
    task = get_task(root, candidate.task_id)
    if task is None:
        return _RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="missing_worktree",
            commit_shas=candidate.commit_shas,
            message="task record is missing",
        )
    if not candidate.worktree_path.exists():
        return _RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="missing_worktree",
            commit_shas=candidate.commit_shas,
            message="recorded worktree is missing",
        )
    worktree_head = _git_stdout(candidate.worktree_path, "rev-parse", "HEAD")
    main_head = current_head(root)
    if not candidate.commit_shas:
        if (
            worktree_head
            and main_head
            and worktree_head != main_head
            and _worktree_patch_already_on_main(root, worktree_head, main_head)
        ):
            _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
            return _RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="already_landed",
                commit_shas=[],
                head_sha=main_head,
                message="worktree patch already landed on main",
            )
        if (
            worktree_head
            and main_head
            and worktree_head != main_head
            and _worktree_has_non_metadata_changes(root, candidate.worktree_path, task.id)
        ):
            _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
            return _RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="already_landed",
                commit_shas=[],
                head_sha=main_head,
                message="worktree patch already landed on main",
            )
        _finalize_rescue(root, task, outcome="no-op", head_sha=main_head)
        return _RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="no_commits",
            commit_shas=[],
            head_sha=main_head,
            message="no worktree commits ahead of main",
        )

    if worktree_head and main_head and _worktree_patch_already_on_main(root, worktree_head, main_head):
        _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
        return _RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="already_landed",
            commit_shas=candidate.commit_shas,
            head_sha=main_head,
            message="worktree patch already landed on main",
        )

    stashed_metadata = _stash_litehive_changes(root)
    for commit_sha in candidate.commit_shas:
        pick = subprocess.run(
            ["git", "cherry-pick", "--no-commit", commit_sha],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if pick.returncode != 0:
            conflicts = _git_lines(root, "diff", "--name-only", "--diff-filter=U")
            metadata_conflicts = [
                path for path in conflicts if _is_task_metadata_path(path, task.id)
            ]
            if conflicts and len(metadata_conflicts) == len(conflicts):
                _resolve_metadata_conflicts(root, metadata_conflicts)
            else:
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                _restore_litehive_changes(root, stashed_metadata)
                save_task(root, task)
                _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
                return _RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="manual_conflict",
                    commit_shas=candidate.commit_shas,
                    message=pick.stderr.strip() or "git cherry-pick failed",
                )

        _drop_task_metadata_changes(root, task.id)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--exit-code"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if staged.returncode == 0:
            continue
        if staged.returncode != 1:
            subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            _restore_litehive_changes(root, stashed_metadata)
            save_task(root, task)
            _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
            return _RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="manual_conflict",
                commit_shas=candidate.commit_shas,
                message="unable to inspect staged rescue changes",
            )

        commit = subprocess.run(
            ["git", "commit", "--reuse-message", commit_sha],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if commit.returncode != 0:
            subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            _restore_litehive_changes(root, stashed_metadata)
            save_task(root, task)
            _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
            return _RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="manual_conflict",
                commit_shas=candidate.commit_shas,
                message=commit.stderr.strip() or "git commit failed after rescue cherry-pick",
            )

    _restore_litehive_changes(root, stashed_metadata)
    head_sha = current_head(root)
    _finalize_rescue(root, task, outcome="rescued", head_sha=head_sha)
    return _RescueResult(
        task_id=candidate.task_id,
        worktree_rel=candidate.worktree_rel,
        status="clean",
        commit_shas=candidate.commit_shas,
        head_sha=head_sha,
        message="rescued onto main",
    )


def _is_litehive_managed_worktree(worktree_rel: str | None) -> bool:
    if not worktree_rel:
        return False
    path = PurePosixPath(worktree_rel)
    return not path.is_absolute() and path.parts[:2] == (".litehive", "worktrees")


def _worktree_commits_ahead_of_main(root: Path, worktree_path: Path) -> list[str]:
    main_head = current_head(root) or "HEAD"
    fork_point = _git_stdout(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return []
    return _git_lines(worktree_path, "rev-list", "--reverse", f"{fork_point}..HEAD")


def _require_clean_main_checkout(root: Path) -> None:
    branch = _git_stdout(root, "branch", "--show-current")
    if branch not in {"main", "master"}:
        raise GitError("worktree rescue --apply requires a clean checkout on branch 'main'")
    if has_non_litehive_changes(root):
        raise GitError("worktree rescue --apply requires a clean checkout on branch 'main'")


def _worktree_patch_already_on_main(root: Path, wt_head: str, main_head: str) -> bool:
    cherry = subprocess.run(
        ["git", "cherry", main_head, wt_head],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if cherry.returncode != 0:
        return False
    lines = [line.strip() for line in cherry.stdout.splitlines() if line.strip()]
    return not lines or all(line.startswith("-") for line in lines)


def _is_task_metadata_path(path: str, task_id: str) -> bool:
    metadata_prefix = f".litehive/tasks/{task_id}-"
    archive_prefix = f".litehive/tasks/archive/{task_id}-"
    return path.startswith(metadata_prefix) or path.startswith(archive_prefix)


def _resolve_metadata_conflicts(root: Path, paths: list[str]) -> None:
    if not paths:
        return
    subprocess.run(
        ["git", "checkout", "--ours", "--", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "add", "--", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _drop_task_metadata_changes(root: Path, task_id: str) -> None:
    changed_paths = _git_lines(root, "diff", "--cached", "--name-only")
    metadata_paths = [path for path in changed_paths if _is_task_metadata_path(path, task_id)]
    if not metadata_paths:
        return
    subprocess.run(
        ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", *metadata_paths],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _finalize_rescue(root: Path, task, *, outcome: str, head_sha: str | None) -> None:
    clear_task_worktree_path(task)
    if outcome in {"rescued", "already-landed", "no-op"}:
        task.status = "done"
        task.pipeline_status = "done"
        set_task_commit_sha(task, head_sha)
    save_task(root, task)
    _clear_unmerged_worktree_state(root, task.id)
    if outcome == "rescued" and head_sha:
        append_journal(root, task, f"Worktree rescue applied onto main at {head_sha}.")
    elif outcome == "already-landed" and head_sha:
        append_journal(root, task, f"Worktree rescue reconciled: patch already landed on main at {head_sha}.")
    else:
        append_journal(root, task, "Worktree rescue found no commits ahead of main; cleared pending rescue state.")


def _clear_unmerged_worktree_state(root: Path, task_id: str) -> None:
    state = load_state(root)
    remaining = [entry for entry in state.unmerged_worktrees if entry.task_id != task_id]
    if len(remaining) == len(state.unmerged_worktrees):
        return
    state.unmerged_worktrees = remaining
    save_state(root, state)


def _ensure_unmerged_worktree_state(root: Path, task_id: str, worktree_rel: str) -> None:
    state = load_state(root)
    for entry in state.unmerged_worktrees:
        if entry.task_id == task_id:
            return
    from litehive.models import UnmergedWorktree

    state.unmerged_worktrees.append(UnmergedWorktree(task_id=task_id, worktree_path=worktree_rel))
    save_state(root, state)


def _git_stdout(root: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _git_lines(root: Path, *args: str) -> list[str]:
    value = _git_stdout(root, *args)
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _stash_litehive_changes(root: Path) -> str | None:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", ".litehive"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or not status.stdout.strip():
        return None
    before = _git_stdout(root, "rev-parse", "-q", "--verify", "refs/stash")
    subprocess.run(
        ["git", "stash", "push", "-u", "-m", "litehive-worktree-rescue", "--", ".litehive"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    after = _git_stdout(root, "rev-parse", "-q", "--verify", "refs/stash")
    if after and after != before:
        return after
    return None


def _restore_litehive_changes(root: Path, stash_ref: str | None) -> None:
    if not stash_ref:
        return
    restored = subprocess.run(
        ["git", "stash", "pop", "--index", stash_ref],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if restored.returncode == 0:
        return
    subprocess.run(
        ["git", "stash", "apply", stash_ref],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "stash", "drop", stash_ref],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _worktree_has_non_metadata_changes(root: Path, worktree_path: Path, task_id: str) -> bool:
    main_head = current_head(root) or "HEAD"
    fork_point = _git_stdout(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return False
    changed_paths = _git_lines(worktree_path, "diff", "--name-only", fork_point, "HEAD")
    return any(not _is_task_metadata_path(path, task_id) for path in changed_paths)
