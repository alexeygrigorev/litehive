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
        self.name = name

    def run(self, state: TaskState) -> Event:
        return Pass()
