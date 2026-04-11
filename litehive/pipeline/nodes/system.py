from abc import abstractmethod

from ..events import (
    CleanState,
    Crash,
    Event,
    NeedsPreExecRecovery,
    Pass,
    PreExecRecoverySucceeded,
    Reject,
)
from ..persistence import TaskState
from ..types import NodeName, NodeType
from .base import Node


class MergeConflict(Exception):
    pass


class GitError(Exception):
    pass


class SystemNode(Node):
    node_type = NodeType.SYSTEM

    def __init__(self, name: NodeName) -> None:
        self.name = name

    @abstractmethod
    def run(self, state: TaskState) -> Event: ...


class ReadyNode(SystemNode):
    """Entry probe for a task. Decides between clean entry and pre-exec recovery.

    M1 placeholder: always emits ``CleanState``. Real implementation will
    check for a stale lock file / missing worktree / corrupted state row
    and emit ``NeedsPreExecRecovery`` in those cases.
    """

    def __init__(self) -> None:
        super().__init__("ready")

    def run(self, state: TaskState) -> Event:
        if self._needs_recovery(state):
            return NeedsPreExecRecovery()
        return CleanState()

    def _needs_recovery(self, state: TaskState) -> bool:
        # Hook: subclasses override to detect actual trouble.
        return False


class PreExecRecoveryNode(SystemNode):
    """Runs pre-execution recovery before the task enters the pipeline proper.

    M1 placeholder: always reports success and routes the task to the same
    stage it would have entered on a clean start. Real implementation will
    clear stale locks, abort partial rebases, and repair missing worktrees.
    """

    def __init__(self) -> None:
        super().__init__("recovering_pre_exec")

    def run(self, state: TaskState) -> Event:
        # M1 placeholder: nothing to fix, resume at grooming (full) /
        # implementing (single) via the transition table.
        resume_stage = "implementing" if state.pipeline_mode.value == "single" else "grooming"
        return PreExecRecoverySucceeded(resume_stage=resume_stage)


class CommitNode(SystemNode):
    """Merges the task worktree into main.

    Subclass and override ``_merge_worktree`` to bind to the real git plumbing.
    """

    def __init__(self) -> None:
        super().__init__("commit")

    def run(self, state: TaskState) -> Event:
        try:
            self._merge_worktree(state)
            return Pass()
        except MergeConflict as exc:
            return Reject(source="system", reason=f"merge conflict: {exc}")
        except GitError as exc:
            return Crash(exc_type="GitError", message=str(exc))

    def _merge_worktree(self, state: TaskState) -> None:
        raise NotImplementedError


class StubCommitNode(CommitNode):
    """Always-pass commit node for tests that don't involve real git.

    Returns ``Pass`` unconditionally. Use the real ``CommitNode`` subclass
    (wired to git + MergeAgent) in production.
    """

    def _merge_worktree(self, state: TaskState) -> None:
        return None
