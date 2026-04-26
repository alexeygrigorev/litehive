from abc import ABC, abstractmethod

from litehive.domain.common import PipelineState
from ..events import Event
from ..persistence import TaskState
from ..types import NodeType


class Node(ABC):
    """A node the machine can be in. Executes itself and returns an Event.

    Tier-1 (retry same session) and tier-2 (switch engine) errors MUST be
    handled inside run() — they never leak out as events. Only tier-3 outcomes
    (Pass / Reject / Blocked / Crash / Timeout / HookOk) are returned.
    """

    name: PipelineState
    node_type: NodeType
    grace_period_seconds: int | None = None

    @abstractmethod
    def run(self, state: TaskState) -> Event: ...


class NodeRegistry:
    """Maps node names to their Node implementations."""

    def __init__(self) -> None:
        self._nodes: dict[PipelineState, Node] = {}

    def register(self, node: Node) -> None:
        if node.name in self._nodes:
            raise ValueError(f"node {node.name!r} already registered")
        self._nodes[node.name] = node

    def get(self, name: PipelineState) -> Node:
        try:
            return self._nodes[name]
        except KeyError as exc:
            raise KeyError(f"no node registered for {name!r}") from exc

    def names(self) -> list[PipelineState]:
        return list(self._nodes)

    def __contains__(self, name: object) -> bool:
        return name in self._nodes
