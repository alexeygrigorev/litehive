"""Worktree subsystem: per-task git worktrees, sync, cleanup, rescue.

The implementation is split across sibling modules so callers can
import only what they need without pulling in the whole graph:

- ``litehive.worktree.paths`` — pure path/branch arithmetic.
- ``litehive.worktree.inspection`` — read-only dirty-state and ownership reports.
- ``litehive.worktree.cleanup`` — ``WorktreeCleanupService`` listing and
  cleanup of managed worktrees.
- ``litehive.worktree.rescue`` — ``WorktreeRescueService`` operator-driven
  cherry-pick onto main.
- ``litehive.worktree.sync`` — ``WorktreeSyncService`` create/reuse/sync flow
  used by lifecycle pre-exec.
- ``litehive.worktree.execution_root`` — ``TaskExecutionRootResolver``
  bootstrap that turns a workspace + task into a usable execution path
  (creating the worktree on first use, rebasing it on subsequent reuse).
"""
