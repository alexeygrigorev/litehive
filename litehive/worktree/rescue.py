"""Rescue operations for stranded task worktrees.

Cherry-picks commits from a flagged task worktree onto main when the normal
merge path failed. Sibling of ``litehive.worktree`` so callers that only need
rescue do not pull in the full WorktreeService graph.
"""

from pathlib import Path

from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.task import TaskRecord, UnmergedWorktree
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.domain.worktree import RescueCandidate, RescueResult
from litehive.git.ops import (
    GitError,
    add_paths,
    cherry_check,
    cherry_pick_abort,
    cherry_pick_no_commit,
    checkout_ours,
    commit_reuse_message,
    current_head,
    has_non_litehive_changes,
    index_has_staged_changes,
    restore_paths,
    rev_parse_verify,
    stash_apply,
    stash_drop,
    stash_pop,
    stash_push,
    stdout_lines as git_stdout_lines,
    stdout_or_none as git_stdout_or_none,
    unmerged_files,
)
from litehive.state.locking import ensure_future_task_mutation_allowed, workspace_lock
from litehive.state.persist import load_state, persist_task_and_state_without_runner_guard, save_state
from litehive.state.records import (
    clear_task_worktree_path,
    get_task,
    get_task_worktree_path,
    list_tasks,
    save_task,
    set_task_commit_sha,
)
from litehive.worktree.paths import is_managed_worktree_path, resolve_recorded_worktree_path


def collect_rescue_candidates(root: Path) -> list[RescueCandidate]:
    """Return tasks flagged ``merge_failed`` whose worktrees still hold commits.

    Used by ``litehive worktree rescue`` (CLI) and ``WorktreeService`` to list
    candidates for an operator-driven cherry-pick onto main.
    """
    candidates: list[RescueCandidate] = []
    for task in list_tasks(root, strict=False):
        if task.status != TaskStatus.FLAGGED or task.flag_reason != "merge_failed":
            continue
        worktree_rel = get_task_worktree_path(task)
        if not is_managed_worktree_path(root, worktree_rel):
            continue
        worktree_path = resolve_recorded_worktree_path(root, worktree_rel)
        if worktree_path is None or worktree_rel is None:
            continue
        if worktree_path.exists():
            commit_shas = _worktree_commits_ahead_of_main(root, worktree_path)
        else:
            commit_shas = []
        candidates.append(
            RescueCandidate(
                task_id=task.id,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                commit_shas=commit_shas,
            )
        )
    return sorted(candidates, key=lambda item: item.task_id)


def require_clean_main_checkout(root: Path) -> None:
    """Refuse to rescue unless main is checked out clean.

    Called by ``WorktreeService.require_clean_main_checkout`` before any
    cherry-pick mutates the main checkout's working tree.
    """
    branch = git_stdout_or_none(root, "branch", "--show-current")
    if branch not in {"main", "master"}:
        raise GitError("worktree rescue --apply requires a clean checkout on branch 'main'")
    if has_non_litehive_changes(root):
        raise GitError("worktree rescue --apply requires a clean checkout on branch 'main'")


def apply_rescue_candidate(root: Path, candidate: RescueCandidate) -> RescueResult:
    """Cherry-pick a candidate's worktree commits onto main and finalize the task.

    Called by ``WorktreeService.apply_rescue_candidate`` per candidate. The
    main checkout must already be clean (see ``require_clean_main_checkout``).
    """
    task = get_task(root, candidate.task_id)
    if task is None:
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="missing_worktree",
            commit_shas=candidate.commit_shas,
            message="task record is missing",
        )
    if not candidate.worktree_path.exists():
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="missing_worktree",
            commit_shas=candidate.commit_shas,
            message="recorded worktree is missing",
        )
    if load_state(root).active_task_id == task.id:
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="active_task",
            commit_shas=candidate.commit_shas,
            message=(f"task {task.id} is still state.active_task_id; worktree rescue refuses to race with the runner"),
        )

    worktree_head = git_stdout_or_none(candidate.worktree_path, "rev-parse", "HEAD")
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
                return RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="active_task",
                    commit_shas=[],
                    message=str(exc),
                )
            return RescueResult(
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
                return RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="active_task",
                    commit_shas=[],
                    message=str(exc),
                )
            return RescueResult(
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
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="active_task",
                commit_shas=[],
                message=str(exc),
            )
        return RescueResult(
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
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="active_task",
                commit_shas=candidate.commit_shas,
                message=str(exc),
            )
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="already_landed",
            commit_shas=candidate.commit_shas,
            head_sha=main_head,
            message="worktree patch already landed on main",
        )

    stashed_metadata = _stash_litehive_changes(root)
    for commit_sha in candidate.commit_shas:
        ok, pick_message = cherry_pick_no_commit(root, commit_sha)
        if not ok:
            conflicts = unmerged_files(root)
            metadata_conflicts = [path for path in conflicts if _is_task_metadata_path(path, task.id)]
            if conflicts and len(metadata_conflicts) == len(conflicts):
                _resolve_metadata_conflicts(root, metadata_conflicts)
            else:
                cherry_pick_abort(root)
                _restore_litehive_changes(root, stashed_metadata)
                save_task(root, task)
                _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
                return RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="manual_conflict",
                    commit_shas=candidate.commit_shas,
                    message=pick_message or "git cherry-pick failed",
                )

        _drop_task_metadata_changes(root, task.id)
        try:
            has_staged = index_has_staged_changes(root)
        except GitError:
            cherry_pick_abort(root)
            _restore_litehive_changes(root, stashed_metadata)
            save_task(root, task)
            _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="manual_conflict",
                commit_shas=candidate.commit_shas,
                message="unable to inspect staged rescue changes",
            )
        if not has_staged:
            continue

        committed, commit_message = commit_reuse_message(root, commit_sha)
        if not committed:
            cherry_pick_abort(root)
            _restore_litehive_changes(root, stashed_metadata)
            save_task(root, task)
            _ensure_unmerged_worktree_state(root, task.id, candidate.worktree_rel)
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="manual_conflict",
                commit_shas=candidate.commit_shas,
                message=commit_message or "git commit failed after rescue cherry-pick",
            )

    _restore_litehive_changes(root, stashed_metadata)
    head_sha = current_head(root)
    try:
        _finalize_rescue(root, task, outcome="rescued", head_sha=head_sha)
    except WorkspaceConflictError as exc:
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="active_task",
            commit_shas=candidate.commit_shas,
            head_sha=head_sha,
            message=str(exc),
        )
    return RescueResult(
        task_id=candidate.task_id,
        worktree_rel=candidate.worktree_rel,
        status="clean",
        commit_shas=candidate.commit_shas,
        head_sha=head_sha,
        message="rescued onto main",
    )


def _worktree_commits_ahead_of_main(root: Path, worktree_path: Path) -> list[str]:
    """Return commit SHAs the worktree carries past its fork point with main, oldest-first."""
    main_head = current_head(root) or "HEAD"
    fork_point = git_stdout_or_none(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return []
    return git_stdout_lines(worktree_path, "rev-list", "--reverse", f"{fork_point}..HEAD")


def _worktree_patch_already_on_main(root: Path, wt_head: str, main_head: str) -> bool:
    """Detect that the worktree's diff is already represented on main so rescue can short-circuit to ``already_landed`` instead of duplicating commits."""
    lines = cherry_check(root, main_head, wt_head)
    if lines is None:
        return False
    return not lines or all(line.startswith("-") for line in lines)


def _is_task_metadata_path(path: str, task_id: str) -> bool:
    """Return True when ``path`` lives under the per-task ``.litehive/tasks/<id>-...`` metadata tree, which rescue treats as droppable noise rather than real source changes."""
    metadata_prefix = f".litehive/tasks/{task_id}-"
    return path.startswith(metadata_prefix)


def _resolve_metadata_conflicts(root: Path, paths: list[str]) -> None:
    """Auto-resolve cherry-pick conflicts that touch only per-task metadata by taking ``ours`` and re-staging, so a metadata-only collision does not stall rescue."""
    if not paths:
        return
    checkout_ours(root, paths)
    try:
        add_paths(root, paths)
    except GitError:
        # Best-effort restage — the rescue flow continues and will surface a
        # manual_conflict result if that fails too.
        pass


def _drop_task_metadata_changes(root: Path, task_id: str) -> None:
    """Strip the task's metadata files out of the staged cherry-pick so the resulting commit only carries real source changes."""
    changed_paths = git_stdout_lines(root, "diff", "--cached", "--name-only")
    metadata_paths = [path for path in changed_paths if _is_task_metadata_path(path, task_id)]
    restore_paths(root, metadata_paths, source="HEAD", staged=True, worktree=True)


def _finalize_rescue(root: Path, task: TaskRecord, outcome: str, head_sha: str | None) -> None:
    """Commit the rescue result to task + workspace state under the workspace lock, refusing if the runner is still pinned to this task."""
    journal_message = "Worktree rescue found no commits ahead of main; cleared pending rescue state."
    if outcome == "rescued" and head_sha:
        journal_message = f"Worktree rescue applied onto main at {head_sha}."
    elif outcome == "already-landed" and head_sha:
        journal_message = f"Worktree rescue reconciled: patch already landed on main at {head_sha}."

    with workspace_lock(root):
        state = load_state(root)
        if state.active_task_id == task.id:
            raise WorkspaceConflictError(
                f"task {task.id} is still state.active_task_id; worktree rescue refuses to race with the runner"
            )
        ensure_future_task_mutation_allowed(root, [task.id], state=state)

        state.unmerged_worktrees = [entry for entry in state.unmerged_worktrees if entry.task_id != task.id]
        clear_task_worktree_path(task)
        if outcome in {"rescued", "already-landed", "no-op"}:
            task.status = TaskStatus.DONE
            task.pipeline_status = PipelineStatus.DONE
            set_task_commit_sha(task, head_sha)
        persist_task_and_state_without_runner_guard(
            root,
            task=task,
            state=state,
            journal_message=journal_message,
        )


def _ensure_unmerged_worktree_state(root: Path, task_id: str, worktree_rel: str) -> None:
    """Re-record the task in ``state.unmerged_worktrees`` after a manual_conflict so the operator still sees it on the next rescue listing."""
    state = load_state(root)
    for entry in state.unmerged_worktrees:
        if entry.task_id == task_id:
            return
    state.unmerged_worktrees.append(UnmergedWorktree(task_id=task_id, worktree_path=worktree_rel))
    save_state(root, state)


def _stash_litehive_changes(root: Path) -> str | None:
    """Set aside any pending ``.litehive/`` workspace edits so the cherry-pick lands on a clean main, returning the stash ref to restore afterwards."""
    if not git_stdout_lines(root, "status", "--porcelain", "--untracked-files=all", "--", ".litehive"):
        return None
    before = rev_parse_verify(root, "refs/stash") or ""
    stash_push(
        root,
        "litehive-worktree-rescue",
        include_untracked=True,
        paths=[".litehive"],
    )
    after = rev_parse_verify(root, "refs/stash") or ""
    if after and after != before:
        return after
    return None


def _restore_litehive_changes(root: Path, stash_ref: str | None) -> None:
    """Reapply the ``.litehive/`` stash created by ``_stash_litehive_changes``, falling back to apply+drop when ``stash pop`` reports a conflict."""
    if not stash_ref:
        return
    ok, _ = stash_pop(root, ref=stash_ref, with_index=True)
    if ok:
        return
    stash_apply(root, stash_ref)
    stash_drop(root, stash_ref)


def _worktree_has_non_metadata_changes(root: Path, worktree_path: Path, task_id: str) -> bool:
    """Return True when the worktree has any real-source diff against main; used to decide whether to mark a no-commit worktree as ``already_landed`` rather than a no-op."""
    main_head = current_head(root) or "HEAD"
    fork_point = git_stdout_or_none(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return False
    changed_paths = git_stdout_lines(worktree_path, "diff", "--name-only", fork_point, "HEAD")
    return any(not _is_task_metadata_path(path, task_id) for path in changed_paths)
