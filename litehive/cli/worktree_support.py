"""Inspect and clean Litehive-managed task worktrees."""

from dataclasses import dataclass
from pathlib import Path
import subprocess

from litehive.git.ops import (
    GitError,
    current_head,
    has_non_litehive_changes,
    status_porcelain,
)
from litehive.state.records import (
    get_task_worktree_path,
    list_tasks,
    set_task_commit_sha,
)
from litehive.tasks.models import WorkspaceConflictError
from litehive.tasks.persistence import load_state, save_state
from litehive.tasks.worktrees import is_managed_worktree_path, resolve_recorded_worktree_path

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


def collect_managed_worktrees(root: Path) -> list[_ManagedWorktree]:
    state = load_state(root)
    active_task = get_task(root, state.active_task_id) if state.active_task_id else None
    active_path = get_task_worktree_path(active_task) if active_task is not None else None

    worktrees: list[_ManagedWorktree] = []
    for task in list_tasks(root):
        worktree_rel = get_task_worktree_path(task)
        if not is_managed_worktree_path(root, worktree_rel):
            continue
        worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
        if worktree_path is None or not worktree_path.exists() or worktree_rel is None:
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
        if not is_managed_worktree_path(root, worktree_rel):
            continue
        worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
        if worktree_path is None or worktree_rel is None:
            continue
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
    if load_state(root).active_task_id == task.id:
        return _RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="active_task",
            commit_shas=candidate.commit_shas,
            message=(
                f"task {task.id} is still state.active_task_id; "
                "worktree rescue refuses to race with the runner"
            ),
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
            try:
                _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
            except WorkspaceConflictError as exc:
                return _RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="active_task",
                    commit_shas=[],
                    message=str(exc),
                )
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
            try:
                _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
            except WorkspaceConflictError as exc:
                return _RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="active_task",
                    commit_shas=[],
                    message=str(exc),
                )
            return _RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="already_landed",
                commit_shas=[],
                head_sha=main_head,
                message="worktree patch already landed on main",
            )
        try:
            _finalize_rescue(root, task, outcome="no-op", head_sha=main_head)
        except WorkspaceConflictError as exc:
            return _RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="active_task",
                commit_shas=[],
                message=str(exc),
            )
        return _RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="no_commits",
            commit_shas=[],
            head_sha=main_head,
            message="no worktree commits ahead of main",
        )

    if worktree_head and main_head and _worktree_patch_already_on_main(root, worktree_head, main_head):
        try:
            _finalize_rescue(root, task, outcome="already-landed", head_sha=main_head)
        except WorkspaceConflictError as exc:
            return _RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="active_task",
                commit_shas=candidate.commit_shas,
                message=str(exc),
            )
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
    try:
        _finalize_rescue(root, task, outcome="rescued", head_sha=head_sha)
    except WorkspaceConflictError as exc:
        return _RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="active_task",
            commit_shas=candidate.commit_shas,
            head_sha=head_sha,
            message=str(exc),
        )
    return _RescueResult(
        task_id=candidate.task_id,
        worktree_rel=candidate.worktree_rel,
        status="clean",
        commit_shas=candidate.commit_shas,
        head_sha=head_sha,
        message="rescued onto main",
    )
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
    from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
    from litehive.state.persist import persist_task_and_state_without_runner_guard

    journal_message = "Worktree rescue found no commits ahead of main; cleared pending rescue state."
    if outcome == "rescued" and head_sha:
        journal_message = f"Worktree rescue applied onto main at {head_sha}."
    elif outcome == "already-landed" and head_sha:
        journal_message = f"Worktree rescue reconciled: patch already landed on main at {head_sha}."

    with workspace_lock(root):
        state = load_state(root)
        if state.active_task_id == task.id:
            raise WorkspaceConflictError(
                f"task {task.id} is still state.active_task_id; "
                "worktree rescue refuses to race with the runner"
            )
        ensure_future_task_mutation_allowed(root, [task.id], state=state)

        state.unmerged_worktrees = [
            entry for entry in state.unmerged_worktrees if entry.task_id != task.id
        ]
        clear_task_worktree_path(task)
        if outcome in {"rescued", "already-landed", "no-op"}:
            task.status = "done"
            task.pipeline_status = "done"
            set_task_commit_sha(task, head_sha)
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=journal_message,
        )


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
    from litehive.models.task_models import UnmergedWorktree

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
