from litehive.domain.pool import (
    DirtyWorktreeFinding,
    DirtyWorktreeGateReport,
    DirtyWorktreeLocationKind,
    DirtyWorktreeOwnership,
)


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
