from litehive.domain.common import PipelineState
from ..events import Event, Pass
from ..persistence import TaskState
from ..types import NodeType
from .base import Node


class TerminalNode(Node):
    """A state the machine rests in; run() is a no-op.

    The Runner never actually calls run() on a terminal — it checks
    ``state.stage in TERMINAL_NODES`` first.
    """

    node_type = NodeType.TERMINAL

    def __init__(self, name: PipelineState) -> None:
        """Bind the terminal to the resting state it represents (DONE / FAILED); the runner uses the ``name`` to identify the terminal in the registry without a separate lookup."""
        self.name = name

    def run(self, state: TaskState) -> Event:
        """
        No-op required by the ``Node`` protocol.

        The runner short-circuits on terminal nodes via ``state.stage
        in TERMINAL_NODES`` and never reaches this body in production;
        the dummy ``Pass()`` exists only so a subclass test that hands
        a TerminalNode to ``run`` does not have to special-case it.
        """
        del state
        return Pass()
