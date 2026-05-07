from litehive.domain.common import PipelineStatus, TaskStatus
from litehive.domain.pool import (
    DirtyWorktreeFinding,
    DirtyWorktreeGateReport,
    DirtyWorktreeLocationKind,
    DirtyWorktreeOwnership,
)
from litehive.domain.task import TaskRecord


def test_dirty_worktree_finding_canonicalizes_persisted_strings() -> None:
    finding = DirtyWorktreeFinding(
        location_kind="task-worktree",
        ownership="task-owned-worktree",
    )

    assert finding.location_kind is DirtyWorktreeLocationKind.TASK_WORKTREE
    assert finding.ownership is DirtyWorktreeOwnership.TASK_OWNED_WORKTREE


def test_dirty_worktree_ownership_owns_pool_blocking_rules() -> None:
    blocking = DirtyWorktreeGateReport(
        findings=[
            DirtyWorktreeFinding(
                location_kind=DirtyWorktreeLocationKind.MAIN_CHECKOUT,
                ownership=DirtyWorktreeOwnership.AMBIGUOUS_OWNERSHIP,
            )
        ]
    )
    task_owned = DirtyWorktreeGateReport(
        findings=[
            DirtyWorktreeFinding(
                location_kind=DirtyWorktreeLocationKind.TASK_WORKTREE,
                ownership=DirtyWorktreeOwnership.TASK_OWNED_WORKTREE,
            )
        ]
    )

    assert blocking.blocks_pool is True
    assert task_owned.blocks_pool is False


def test_task_record_owns_pool_summary_status_buckets() -> None:
    queued = TaskRecord(id="T-0001", slug="queued", title="Queued")
    interrupted = TaskRecord(
        id="T-0002",
        slug="interrupted",
        title="Interrupted",
        status=TaskStatus.INTERRUPTED,
        pipeline_status=PipelineStatus.IMPLEMENTING,
    )
    done_interrupted = TaskRecord(
        id="T-0003",
        slug="done-interrupted",
        title="Done interrupted",
        status=TaskStatus.INTERRUPTED,
        pipeline_status=PipelineStatus.DONE,
    )
    closed = TaskRecord(
        id="T-0004",
        slug="closed",
        title="Closed",
        status=TaskStatus.CLOSED,
    )

    assert queued.is_pool_pending is True
    assert interrupted.is_resumable is True
    assert done_interrupted.is_resumable is False
    assert closed.is_closed is True
