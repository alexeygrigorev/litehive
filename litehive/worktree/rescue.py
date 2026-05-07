"""
Operator-driven rescue of stranded task worktrees onto main.

When the normal merge path fails (the lifecycle layer flags the task
``merge_failed``), the work is still on the task's worktree branch.
The rescue flow cherry-picks those commits onto main so the operator
doesn't lose the work, with conflict detection and metadata-only
auto-resolution. Sibling of ``litehive.worktree`` so callers that
only need rescue don't pull in the full ``WorktreeService`` graph.
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
from litehive.state.locking import ensure_future_task_mutation_allowed_for_workspace, workspace_lock_for_workspace
from litehive.state.persist import (
    load_state_for_workspace,
    persist_task_and_state_without_runner_guard_for_workspace,
    save_state_for_workspace,
)
from litehive.state.records import (
    clear_task_worktree_path,
    get_task_worktree_path,
    set_task_commit_sha,
)
from litehive.worktree.paths import is_managed_worktree_path_for_workspace, resolve_recorded_worktree_path_for_workspace
from litehive.workspace import Workspace


def collect_rescue_candidates_for_workspace(workspace: Workspace) -> list[RescueCandidate]:
    """
    Return tasks flagged ``merge_failed`` whose worktrees still hold commits.

    Used by ``litehive worktree rescue`` (CLI) and ``WorktreeService`` to
    list candidates for an operator-driven cherry-pick onto main.
    Sorted by task id so the CLI output is stable and the operator
    can re-invoke against the same ordering.
    """
    candidates: list[RescueCandidate] = []
    for task in workspace.list_tasks(strict=False):
        if task.status != TaskStatus.FLAGGED or task.flag_reason != "merge_failed":
            continue
        worktree_rel = get_task_worktree_path(task)
        if not is_managed_worktree_path_for_workspace(workspace, worktree_rel):
            continue
        worktree_path = resolve_recorded_worktree_path_for_workspace(workspace, worktree_rel)
        if worktree_path is None or worktree_rel is None:
            continue
        if worktree_path.exists():
            commit_shas = _worktree_commits_ahead_of_main(workspace.root, worktree_path)
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


def require_clean_main_checkout_for_workspace(workspace: Workspace) -> None:
    """
    Refuse to rescue unless ``main`` is checked out clean.

    Called by ``WorktreeService.require_clean_main_checkout`` before
    any cherry-pick mutates the main checkout's working tree —
    rescuing onto a dirty main would mix unrelated edits into the
    rescued commit and confuse later git history. Raises ``GitError``
    so the rescue CLI surfaces the precondition failure rather than
    silently performing a half-rescue.
    """
    branch = git_stdout_or_none(workspace.root, "branch", "--show-current")
    if branch not in {"main", "master"}:
        raise GitError("worktree rescue --apply requires a clean checkout on branch 'main'")
    if has_non_litehive_changes(workspace.root):
        raise GitError("worktree rescue --apply requires a clean checkout on branch 'main'")


def apply_rescue_candidate_for_workspace(workspace: Workspace, candidate: RescueCandidate) -> RescueResult:
    """
    Cherry-pick a candidate's commits onto main and finalize the task record.

    Called once per candidate by ``WorktreeService.apply_rescue_candidate``.
    Caller must have run :func:`require_clean_main_checkout` first;
    this function will not enforce that itself because the rescue CLI
    runs the check once for the whole batch. Returns a
    ``RescueResult`` with the high-level outcome (``clean``,
    ``already_landed``, ``manual_conflict``, …) so the CLI can render
    a per-row status table.
    """
    task = workspace.get_task(candidate.task_id)
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
    if load_state_for_workspace(workspace).active_task_id == task.id:
        return RescueResult(
            task_id=candidate.task_id,
            worktree_rel=candidate.worktree_rel,
            status="active_task",
            commit_shas=candidate.commit_shas,
            message=(f"task {task.id} is still state.active_task_id; worktree rescue refuses to race with the runner"),
        )

    worktree_head = git_stdout_or_none(candidate.worktree_path, "rev-parse", "HEAD")
    main_head = current_head(workspace.root)

    if not candidate.commit_shas:
        if (
            worktree_head
            and main_head
            and worktree_head != main_head
            and _worktree_patch_already_on_main(workspace.root, worktree_head, main_head)
        ):
            try:
                _finalize_rescue_for_workspace(workspace, task, outcome="already-landed", head_sha=main_head)
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
            and _worktree_has_non_metadata_changes(workspace.root, candidate.worktree_path, task.id)
        ):
            try:
                _finalize_rescue_for_workspace(workspace, task, outcome="already-landed", head_sha=main_head)
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
            _finalize_rescue_for_workspace(workspace, task, outcome="no-op", head_sha=main_head)
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

    if worktree_head and main_head and _worktree_patch_already_on_main(workspace.root, worktree_head, main_head):
        try:
            _finalize_rescue_for_workspace(workspace, task, outcome="already-landed", head_sha=main_head)
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

    stashed_metadata = _stash_litehive_changes(workspace.root)
    for commit_sha in candidate.commit_shas:
        ok, pick_message = cherry_pick_no_commit(workspace.root, commit_sha)
        if not ok:
            conflicts = unmerged_files(workspace.root)
            metadata_conflicts = [path for path in conflicts if _is_task_metadata_path(path, task.id)]
            if conflicts and len(metadata_conflicts) == len(conflicts):
                _resolve_metadata_conflicts(workspace.root, metadata_conflicts)
            else:
                cherry_pick_abort(workspace.root)
                _restore_litehive_changes(workspace.root, stashed_metadata)
                workspace.save_task(task)
                _ensure_unmerged_worktree_state_for_workspace(workspace, task.id, candidate.worktree_rel)
                return RescueResult(
                    task_id=candidate.task_id,
                    worktree_rel=candidate.worktree_rel,
                    status="manual_conflict",
                    commit_shas=candidate.commit_shas,
                    message=pick_message or "git cherry-pick failed",
                )

        _drop_task_metadata_changes(workspace.root, task.id)
        try:
            has_staged = index_has_staged_changes(workspace.root)
        except GitError:
            cherry_pick_abort(workspace.root)
            _restore_litehive_changes(workspace.root, stashed_metadata)
            workspace.save_task(task)
            _ensure_unmerged_worktree_state_for_workspace(workspace, task.id, candidate.worktree_rel)
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="manual_conflict",
                commit_shas=candidate.commit_shas,
                message="unable to inspect staged rescue changes",
            )
        if not has_staged:
            continue

        committed, commit_message = commit_reuse_message(workspace.root, commit_sha)
        if not committed:
            cherry_pick_abort(workspace.root)
            _restore_litehive_changes(workspace.root, stashed_metadata)
            workspace.save_task(task)
            _ensure_unmerged_worktree_state_for_workspace(workspace, task.id, candidate.worktree_rel)
            return RescueResult(
                task_id=candidate.task_id,
                worktree_rel=candidate.worktree_rel,
                status="manual_conflict",
                commit_shas=candidate.commit_shas,
                message=commit_message or "git commit failed after rescue cherry-pick",
            )

    _restore_litehive_changes(workspace.root, stashed_metadata)
    head_sha = current_head(workspace.root)
    try:
        _finalize_rescue_for_workspace(workspace, task, outcome="rescued", head_sha=head_sha)
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
    """
    Return commit SHAs the worktree carries past its fork-point with main, oldest-first.

    The cherry-pick loop in :func:`apply_rescue_candidate` replays
    these in order; oldest-first preserves the original commit
    order so the rescued history reads naturally. Empty result
    means the worktree has no commits to rescue (the
    ``no_commits`` / ``already_landed`` outcomes branch on this).
    """
    main_head = current_head(root) or "HEAD"
    fork_point = git_stdout_or_none(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return []
    return git_stdout_lines(worktree_path, "rev-list", "--reverse", f"{fork_point}..HEAD")


def _worktree_patch_already_on_main(root: Path, wt_head: str, main_head: str) -> bool:
    """
    True when the worktree's diff is already represented on main.

    Detects the "the operator already merged this manually" case so
    the rescue flow short-circuits to ``already_landed`` rather than
    cherry-picking equivalent commits onto main twice. Backed by
    ``git cherry``, which compares patch ids rather than commit shas.
    """
    lines = cherry_check(root, main_head, wt_head)
    if lines is None:
        return False
    return not lines or all(line.startswith("-") for line in lines)


def _is_task_metadata_path(path: str, task_id: str) -> bool:
    """
    True when ``path`` lives under the per-task ``.litehive/tasks/<id>-...`` tree.

    Per-task metadata changes are bookkeeping noise that should not
    survive into the rescued commit — they describe how the task
    ran, not what it changed in the codebase. Used both to drop
    metadata from staged cherry-picks and to auto-resolve metadata-
    only conflicts.
    """
    metadata_prefix = f".litehive/tasks/{task_id}-"
    return path.startswith(metadata_prefix)


def _resolve_metadata_conflicts(root: Path, paths: list[str]) -> None:
    """
    Auto-resolve metadata-only cherry-pick conflicts by taking ``ours``.

    Metadata-only collisions are noise — the rescue flow will drop
    them entirely from the resulting commit anyway, so making the
    operator resolve them by hand would be busywork that stalls
    every metadata-only cherry-pick. Failing to re-stage is
    best-effort because the surrounding flow already handles the
    "manual_conflict" outcome.
    """
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
    """
    Strip the task's metadata files out of the staged cherry-pick.

    Called between ``cherry-pick --no-commit`` and the actual
    ``git commit``, so the resulting rescue commit only carries
    real source changes — keeping the metadata would dirty the
    rescued history with churn that means nothing on main.
    """
    changed_paths = git_stdout_lines(root, "diff", "--cached", "--name-only")
    metadata_paths = [path for path in changed_paths if _is_task_metadata_path(path, task_id)]
    restore_paths(root, metadata_paths, source="HEAD", staged=True, worktree=True)


def _finalize_rescue_for_workspace(workspace: Workspace, task: TaskRecord, outcome: str, head_sha: str | None) -> None:
    """
    Commit the rescue result to task + workspace state under the workspace lock.

    Mutating both the task and the workspace ``unmerged_worktrees``
    list as one atomic unit prevents a half-finalized rescue (task
    marked done but unmerged-worktrees still listing it). Refuses
    if the runner is still pinned to this task — racing the live
    runner would corrupt task state — and writes a journal entry so
    the operator can audit the outcome.
    """
    journal_message = "Worktree rescue found no commits ahead of main; cleared pending rescue state."
    if outcome == "rescued" and head_sha:
        journal_message = f"Worktree rescue applied onto main at {head_sha}."
    elif outcome == "already-landed" and head_sha:
        journal_message = f"Worktree rescue reconciled: patch already landed on main at {head_sha}."

    with workspace_lock_for_workspace(workspace):
        state = load_state_for_workspace(workspace)
        if state.active_task_id == task.id:
            raise WorkspaceConflictError(
                f"task {task.id} is still state.active_task_id; worktree rescue refuses to race with the runner"
            )
        ensure_future_task_mutation_allowed_for_workspace(workspace, [task.id], state=state)

        state.unmerged_worktrees = [entry for entry in state.unmerged_worktrees if entry.task_id != task.id]
        clear_task_worktree_path(task)
        if outcome in {"rescued", "already-landed", "no-op"}:
            task.status = TaskStatus.DONE
            task.pipeline_status = PipelineStatus.DONE
            set_task_commit_sha(task, head_sha)
        persist_task_and_state_without_runner_guard_for_workspace(
            workspace,
            task=task,
            state=state,
            journal_message=journal_message,
        )


def _ensure_unmerged_worktree_state_for_workspace(workspace: Workspace, task_id: str, worktree_rel: str) -> None:
    """
    Re-record the task in ``state.unmerged_worktrees`` after a manual_conflict.

    Without this, a partial cherry-pick that aborts halfway would
    drop the task from the unmerged list and the operator's next
    rescue listing wouldn't show it as still needing attention. The
    idempotent check (skip when already present) keeps repeated
    rescue attempts from duplicating the entry.
    """
    state = load_state_for_workspace(workspace)
    for entry in state.unmerged_worktrees:
        if entry.task_id == task_id:
            return
    state.unmerged_worktrees.append(UnmergedWorktree(task_id=task_id, worktree_path=worktree_rel))
    save_state_for_workspace(workspace, state)


def _stash_litehive_changes(root: Path) -> str | None:
    """
    Set pending ``.litehive/`` workspace edits aside before the cherry-pick.

    The rescue cherry-pick must land on a clean tree, but the daemon
    or another command may have left ``.litehive/`` metadata edits
    pending; without stashing them the cherry-pick would refuse or
    splice metadata into the rescued commit. Returns the stash ref
    so :func:`_restore_litehive_changes` can pop exactly that
    entry afterwards.
    """
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
    """
    Reapply the ``.litehive/`` stash captured by :func:`_stash_litehive_changes`.

    Falls back to apply-then-drop when ``stash pop`` reports a
    conflict — pop refuses to drop the stash on conflict, but the
    rescue flow has already done the destructive work and we want
    the stash gone either way (the operator can recover from
    reflog if a recoverable conflict actually mattered).
    """
    if not stash_ref:
        return
    ok, _ = stash_pop(root, ref=stash_ref, with_index=True)
    if ok:
        return
    stash_apply(root, stash_ref)
    stash_drop(root, stash_ref)


def _worktree_has_non_metadata_changes(root: Path, worktree_path: Path, task_id: str) -> bool:
    """
    True when the worktree's diff against main contains anything besides task metadata.

    Used by :func:`apply_rescue_candidate` to distinguish "a worktree
    with real changes the operator already landed on main"
    (``already_landed``) from "a worktree that never had real
    changes" (``no_commits`` no-op). Without this, a metadata-only
    worktree would falsely render as already_landed and confuse the
    rescue summary.
    """
    main_head = current_head(root) or "HEAD"
    fork_point = git_stdout_or_none(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return False
    changed_paths = git_stdout_lines(worktree_path, "diff", "--name-only", fork_point, "HEAD")
    return any(not _is_task_metadata_path(path, task_id) for path in changed_paths)
