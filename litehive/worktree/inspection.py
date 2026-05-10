"""
Read-only diagnosis of dirty worktree state and task ownership.

Walks the main checkout and every task worktree, classifying findings
by ownership (main-checkout dirt, task-owned, ambiguous,
missing-recorded). The pool gate refuses to proceed on certain
ownership classes; ``litehive workspace status`` and
``WorktreeInspector.inspect_task_worktree`` render the same data for
the operator. Read-only by design — repair flows live elsewhere so
status code never accidentally mutates state.
"""

from pathlib import Path, PurePosixPath

from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.pool import (
    DirtyWorktreeFinding,
    DirtyWorktreeGateReport,
    DirtyWorktreeLocationKind,
    DirtyWorktreeOwnership,
)
from litehive.domain.task import TaskRecord
from litehive.domain.worktree import TaskWorktreeInspection
from litehive.git.ops import (
    GitError,
    current_head,
    is_git_repo,
    status_porcelain,
    stdout_lines as git_stdout_lines,
    stdout_or_none as git_stdout_or_none,
)
from litehive.state.records import get_task_worktree_path, WorkspaceTasks
from litehive.tasks.activity import task_activity_store_for_task
from litehive.tasks.activity_rendering import normalized_files_changed
from litehive.workspace import Workspace
from litehive.worktree.paths import WorktreePaths


class WorktreeInspector:
    """
    Read-only task worktree inspection for status and debug surfaces.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the inspector to one workspace and its path policy.

        The inspector holds a ``WorktreePaths`` instance so every call
        shares the same managed-path checks and resolution logic rather
        than each method reconstructing a ``WorktreePaths`` on the fly.
        """
        self.workspace = workspace
        self.paths = WorktreePaths(workspace)

    def inspect_task_worktree(self, task: TaskRecord) -> TaskWorktreeInspection:
        """
        Snapshot a task's recorded worktree state without mutating metadata.

        Bundles existence, uncommitted changes, and committed-past-main paths
        into one domain record so CLI/debug callers can render a stable view
        without knowing the git commands behind it.
        """
        worktree_rel = get_task_worktree_path(task)
        worktree_path = self.paths.resolve_recorded_worktree_path(worktree_rel)
        if worktree_rel is None or worktree_path is None or not worktree_path.exists():
            return TaskWorktreeInspection(
                task_id=task.id,
                worktree_rel=worktree_rel,
                worktree_path=worktree_path,
                exists=False,
                uncommitted=[],
                committed_ahead_of_main=[],
            )
        return TaskWorktreeInspection(
            task_id=task.id,
            worktree_rel=worktree_rel,
            worktree_path=worktree_path,
            exists=True,
            uncommitted=worktree_uncommitted_changes(worktree_path),
            committed_ahead_of_main=self.committed_changes(worktree_path),
        )

    def inspect_dirty_gate(self) -> DirtyWorktreeGateReport:
        """
        Build the operator-facing dirty-worktree report.
        """
        if not is_git_repo(self.workspace.root):
            return DirtyWorktreeGateReport()

        findings: list[DirtyWorktreeFinding] = []
        try:
            dirty_entries = status_porcelain(self.workspace.root)
        except GitError:
            return DirtyWorktreeGateReport()

        tasks = WorkspaceTasks(self.workspace).list(strict=False)
        if dirty_entries:
            owners = [
                task for task in tasks if _task_can_resume_with_owned_dirty_paths(self.workspace, task, dirty_entries)
            ]
            finding = DirtyWorktreeFinding(
                location_kind=DirtyWorktreeLocationKind.MAIN_CHECKOUT,
                ownership=DirtyWorktreeOwnership.MAIN_CHECKOUT,
                dirty_paths=dirty_entry_paths(dirty_entries),
            )
            if len(owners) == 1:
                finding.ownership = DirtyWorktreeOwnership.TASK_OWNED
                finding.task_id = owners[0].id
                finding.worktree_path = get_task_worktree_path(owners[0])
            elif len(owners) > 1:
                finding.ownership = DirtyWorktreeOwnership.AMBIGUOUS_OWNERSHIP
                finding.task_id = ",".join(task.id for task in owners)
            findings.append(finding)

        for task in tasks:
            worktree_path = self.paths.resolve_recorded_worktree_path(get_task_worktree_path(task))
            if worktree_path is None:
                continue
            recorded_path = get_task_worktree_path(task)
            if not worktree_path.exists():
                findings.append(
                    DirtyWorktreeFinding(
                        location_kind=DirtyWorktreeLocationKind.TASK_WORKTREE,
                        ownership=DirtyWorktreeOwnership.MISSING_RECORDED_WORKTREE,
                        task_id=task.id,
                        worktree_path=recorded_path,
                    )
                )
                continue
            try:
                worktree_dirty_entries = status_porcelain(worktree_path)
            except GitError:
                findings.append(
                    DirtyWorktreeFinding(
                        location_kind=DirtyWorktreeLocationKind.TASK_WORKTREE,
                        ownership=DirtyWorktreeOwnership.MISSING_RECORDED_WORKTREE,
                        task_id=task.id,
                        worktree_path=recorded_path,
                    )
                )
                continue
            if not worktree_dirty_entries:
                continue
            findings.append(
                DirtyWorktreeFinding(
                    location_kind=DirtyWorktreeLocationKind.TASK_WORKTREE,
                    ownership=DirtyWorktreeOwnership.TASK_OWNED_WORKTREE,
                    task_id=task.id,
                    worktree_path=recorded_path,
                    dirty_paths=dirty_entry_paths(worktree_dirty_entries),
                )
            )

        return DirtyWorktreeGateReport(findings=findings)

    def committed_changes(self, worktree_path: Path) -> list[str]:
        """
        Return sorted unique paths committed past main on the worktree branch.
        """
        main_head = current_head(self.workspace.root) or "HEAD"
        fork_point = git_stdout_or_none(worktree_path, "merge-base", main_head, "HEAD")
        if not fork_point:
            return []
        return sorted(set(git_stdout_lines(worktree_path, "diff", "--name-only", fork_point, "HEAD")))


def dirty_entry_paths(dirty_entries: list[str]) -> list[str]:
    """
    Strip status codes off ``git status --porcelain`` lines and return paths.

    Centralized so the porcelain-parsing rules (status code prefix,
    quoted paths with embedded escapes, rename arrows) live in one
    place — ``WorktreeInspector.inspect_dirty_gate`` and
    ``worktree_uncommitted_changes`` both rely on the same parsing
    or they would disagree about which paths count as dirty.
    """
    paths: list[str] = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1].strip()
        if raw:
            paths.append(raw)
    return paths


def worktree_uncommitted_changes(worktree_path: Path) -> list[str]:
    """
    Return sorted unique uncommitted paths in the worktree.

    Returns ``[]`` when git fails, so a transient git error doesn't
    crash the status code path. Called by
    ``WorktreeInspector.inspect_task_worktree`` when building a
    ``TaskWorktreeInspection`` — the sort + dedupe means the
    operator output is stable across reruns.
    """
    try:
        return sorted(set(dirty_entry_paths(status_porcelain(worktree_path))))
    except GitError:
        return []


def _allowed_commit_paths(workspace: Workspace, task: TaskRecord) -> set[PurePosixPath]:
    """
    Compute the paths an interrupted task may legitimately leave dirty.

    A resumable interruption can only "own" dirt the task already
    declared: its per-task metadata directory plus every path its
    activity log has previously listed as changed. Anything else on
    disk belongs to a different task or to the operator, so the
    gate refuses to attribute it to this task.
    """
    paths: set[PurePosixPath] = set()
    paths.add(PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}")
    for entry in task_activity_store_for_task(workspace, task).load():
        for changed_file in normalized_files_changed(entry.files_changed):
            paths.add(PurePosixPath(changed_file))
    return paths


def _unexpected_dirty_paths(
    dirty_entries: list[str],
    allowed_paths: set[PurePosixPath],
) -> list[str]:
    """
    Return the paths a resuming task didn't already claim ownership of.

    Called by ``_task_can_resume_with_owned_dirty_paths`` to decide
    whether an interrupted task is the legitimate owner of the dirty
    main checkout. Ignores ``$tmpdir`` and ``/tmp/`` noise from
    pytest, and ignores ``.litehive/`` paths the task didn't list —
    those belong to other tasks and shouldn't disqualify this one.
    """
    unexpected = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if not raw:
            continue
        if "$tmpdir" in raw or raw.startswith("/tmp/"):
            continue
        if raw.startswith(".litehive/"):
            if not _path_is_within_allowed_paths(raw, allowed_paths):
                continue
        if _path_is_within_allowed_paths(raw, allowed_paths):
            continue
        unexpected.append(raw)
    return unexpected


def _path_is_within_allowed_paths(raw: str, allowed_paths) -> bool:
    """
    True when ``raw`` exactly equals or sits beneath any allowed path.

    Compares each ``allowed_paths`` entry as both an exact match and
    a directory prefix so ``foo/bar`` is recognized as inside an
    allowed ``foo`` entry. Caller:
    :func:`_unexpected_dirty_paths_outside_task_scope`.
    """
    for path in allowed_paths:
        path_str = str(path)
        if raw == path_str:
            return True
        if raw.startswith(f"{path_str}/"):
            return True
    return False


def _task_can_resume_with_owned_dirty_paths(
    workspace: Workspace,
    task: TaskRecord,
    dirty_entries: list[str],
) -> bool:
    """
    True when an interrupted task can plausibly own the dirty main checkout.

    Used by ``WorktreeInspector.inspect_dirty_gate`` to disambiguate which
    task should resume when the main checkout has uncommitted
    changes. The task must be ``INTERRUPTED`` (terminal tasks
    obviously can't resume), out of the backlog/done buckets, and
    every dirty path must be inside its declared scope.
    """
    if task.status != TaskStatus.INTERRUPTED:
        return False
    if task.pipeline_status in {PipelineStatus.BACKLOG, PipelineStatus.DONE}:
        return False
    return not _unexpected_dirty_paths(dirty_entries, _allowed_commit_paths(workspace, task))
