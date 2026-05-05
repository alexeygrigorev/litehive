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
        """Bind the terminal to the resting state it represents (DONE / FAILED) so the runner can identify it without a separate registry."""
        self.name = name

    def run(self, state: TaskState) -> Event:
        """No-op required by the ``Node`` protocol; the runner short-circuits on terminal nodes via ``state.stage in TERMINAL_NODES`` and never reaches this body in production."""
        del state
        return Pass()
