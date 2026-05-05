"""
Read-only diagnosis of dirty worktree state and task ownership.

Walks the main checkout and every task worktree, classifying findings
by ownership (main-checkout dirt, task-owned, ambiguous,
missing-recorded). The pool gate refuses to proceed on certain
ownership classes; ``litehive workspace status`` and
``WorktreeService.inspect_task_worktree`` render the same data for
the operator. Read-only by design — repair flows live elsewhere so
status code never accidentally mutates state.
"""

from pathlib import Path, PurePosixPath

from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.pool import DirtyWorktreeFinding, DirtyWorktreeGateReport
from litehive.domain.task import TaskRecord
from litehive.git.ops import (
    GitError,
    current_head,
    is_git_repo,
    status_porcelain,
    stdout_lines as git_stdout_lines,
    stdout_or_none as git_stdout_or_none,
)
from litehive.state.records import get_task_worktree_path, list_tasks
from litehive.tasks.activity import load_task_activity
from litehive.tasks.activity_rendering import normalized_files_changed
from litehive.workspace import Workspace
from litehive.worktree.paths import resolve_recorded_worktree_path


def inspect_dirty_worktree_gate(root: Path) -> DirtyWorktreeGateReport:
    """
    Build the operator-facing dirty-worktree report.

    The pool gate consults ``DirtyWorktreeGateReport.blocks_pool``
    before letting a new task claim the workspace, so "what's dirty
    and who owns it" must be a single read-only snapshot. Same data
    drives ``litehive workspace status`` and the lifecycle tests for
    interrupted-task resumption — three call sites means one helper
    instead of three.
    """
    if not is_git_repo(root):
        return DirtyWorktreeGateReport()

    findings: list[DirtyWorktreeFinding] = []
    try:
        dirty_entries = status_porcelain(root)
    except GitError:
        return DirtyWorktreeGateReport()

    tasks = list_tasks(root, strict=False)
    if dirty_entries:
        owners = [task for task in tasks if _task_can_resume_with_owned_dirty_paths(root, task, dirty_entries)]
        finding = DirtyWorktreeFinding(
            location_kind="main-checkout",
            ownership="main-checkout",
            dirty_paths=dirty_entry_paths(dirty_entries),
        )
        if len(owners) == 1:
            finding.ownership = "task-owned"
            finding.task_id = owners[0].id
            finding.worktree_path = get_task_worktree_path(owners[0])
        elif len(owners) > 1:
            finding.ownership = "ambiguous-ownership"
            finding.task_id = ",".join(task.id for task in owners)
        findings.append(finding)

    for task in tasks:
        worktree_path = resolve_recorded_worktree_path(root, get_task_worktree_path(task))
        if worktree_path is None:
            continue
        recorded_path = get_task_worktree_path(task)
        if not worktree_path.exists():
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
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
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=recorded_path,
                )
            )
            continue
        if not worktree_dirty_entries:
            continue
        findings.append(
            DirtyWorktreeFinding(
                location_kind="task-worktree",
                ownership="task-owned-worktree",
                task_id=task.id,
                worktree_path=recorded_path,
                dirty_paths=dirty_entry_paths(worktree_dirty_entries),
            )
        )

    return DirtyWorktreeGateReport(findings=findings)


def dirty_entry_paths(dirty_entries: list[str]) -> list[str]:
    """
    Strip status codes off ``git status --porcelain`` lines and return paths.

    Centralized so the porcelain-parsing rules (status code prefix,
    quoted paths with embedded escapes, rename arrows) live in one
    place — ``inspect_dirty_worktree_gate`` and
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
    ``WorktreeService.inspect_task_worktree`` when building a
    ``TaskWorktreeInspection`` — the sort + dedupe means the
    operator output is stable across reruns.
    """
    try:
        return sorted(set(dirty_entry_paths(status_porcelain(worktree_path))))
    except GitError:
        return []


def worktree_committed_changes(root: Path, worktree_path: Path) -> list[str]:
    """
    Return sorted unique paths committed past main on the worktree branch.

    Pairs with :func:`worktree_uncommitted_changes`: together they
    form the "what has this task changed?" view rendered by
    ``TaskWorktreeInspection``. Returns ``[]`` when there's no
    fork-point with main (a fresh worktree that never diverged
    has nothing committed past main).
    """
    main_head = current_head(root) or "HEAD"
    fork_point = git_stdout_or_none(worktree_path, "merge-base", main_head, "HEAD")
    if not fork_point:
        return []
    return sorted(set(git_stdout_lines(worktree_path, "diff", "--name-only", fork_point, "HEAD")))


def _allowed_commit_paths(root: Path, task: TaskRecord) -> set[PurePosixPath]:
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
    for entry in load_task_activity(Workspace.from_path(root), task):
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
            if not any(raw == str(path) or raw.startswith(f"{path}/") for path in allowed_paths):
                continue
        if any(raw == str(path) or raw.startswith(f"{path}/") for path in allowed_paths):
            continue
        unexpected.append(raw)
    return unexpected


def _task_can_resume_with_owned_dirty_paths(
    root: Path,
    task: TaskRecord,
    dirty_entries: list[str],
) -> bool:
    """
    True when an interrupted task can plausibly own the dirty main checkout.

    Used by ``inspect_dirty_worktree_gate`` to disambiguate which
    task should resume when the main checkout has uncommitted
    changes. The task must be ``INTERRUPTED`` (terminal tasks
    obviously can't resume), out of the backlog/done buckets, and
    every dirty path must be inside its declared scope.
    """
    if task.status != TaskStatus.INTERRUPTED:
        return False
    if task.pipeline_status in {PipelineStatus.BACKLOG, PipelineStatus.DONE}:
        return False
    return not _unexpected_dirty_paths(dirty_entries, _allowed_commit_paths(root, task))
