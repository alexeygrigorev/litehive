from abc import abstractmethod

from ..events import Crash, Event, Pass, Reject
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
