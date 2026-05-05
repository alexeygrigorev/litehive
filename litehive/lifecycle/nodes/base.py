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
    def run(self, state: TaskState) -> Event:
        """Execute the node and return a tier-3 outcome event.

        The runner calls this once per visit to advance the lifecycle machine.
        Implementations swallow tier-1/tier-2 retries internally and only
        surface terminal outcomes (Pass / Reject / Blocked / Crash / Timeout /
        HookOk) so the transition table can decide the next state.
        """
        ...


class NodeRegistry:
    """Maps node names to their Node implementations."""

    def __init__(self) -> None:
        """Create an empty registry; populated by the lifecycle bootstrap that imports each node module."""
        self._nodes: dict[PipelineState, Node] = {}

    def register(self, node: Node) -> None:
        """Register a node implementation under its declared ``name``.

        Called once per node module at import time by the lifecycle setup.
        Rejects duplicate registrations so two builds of the same node cannot
        silently shadow each other.
        """
        if node.name in self._nodes:
            raise ValueError(f"node {node.name!r} already registered")
        self._nodes[node.name] = node

    def get(self, name: PipelineState) -> Node:
        """Return the Node bound to ``name`` or raise KeyError with a registry-aware message.

        The runner uses this to look up the node for the task's current
        pipeline state on every machine step.
        """
        try:
            return self._nodes[name]
        except KeyError as exc:
            raise KeyError(f"no node registered for {name!r}") from exc

    def names(self) -> list[PipelineState]:
        """Return all registered node names; used by diagnostics and registry-validation tests."""
        return list(self._nodes)

    def __contains__(self, name: object) -> bool:
        """Support ``name in registry`` checks used by the transition validator."""
        return name in self._nodes
