from dataclasses import dataclass
from enum import Enum

from ..events import Event, HookOk, Reject
from ..persistence import TaskState
from ..types import NodeName, NodeType
from .base import Node


class ExecutionMode(str, Enum):
    FAIL_FAST = "fail_fast"
    RUN_ALL = "run_all"


@dataclass
class HookSpec:
    command: str
    reject_on_failure: bool = True
    timeout_seconds: int = 60


@dataclass
class HookResult:
    spec: HookSpec
    ok: bool
    output: str = ""


class HookRunner:
    """Abstraction over the actual shell execution. Swap in tests."""

    def run(self, spec: HookSpec, state: TaskState) -> HookResult:
        raise NotImplementedError


class HookNode(Node):
    node_type = NodeType.HOOK

    def __init__(
        self,
        name: NodeName,
        hooks: list[HookSpec],
        runner: HookRunner,
        *,
        execution_mode: ExecutionMode = ExecutionMode.FAIL_FAST,
    ) -> None:
        self.name = name
        self.hooks = hooks
        self.runner = runner
        self.execution_mode = execution_mode

    def run(self, state: TaskState) -> Event:
        results: list[HookResult] = []
        for spec in self.hooks:
            result = self.runner.run(spec, state)
            results.append(result)
            if (
                self.execution_mode == ExecutionMode.FAIL_FAST
                and not result.ok
                and spec.reject_on_failure
            ):
                break
        rejecting = [r for r in results if not r.ok and r.spec.reject_on_failure]
        if rejecting:
            return Reject(source="hook", reason=self._summarize(rejecting))
        return HookOk()

    @staticmethod
    def _summarize(results: list[HookResult]) -> str:
        return "; ".join(f"{r.spec.command}: {r.output}".strip() for r in results)
