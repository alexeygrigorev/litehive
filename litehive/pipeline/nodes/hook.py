import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

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


class SubprocessHookRunner(HookRunner):
    """Runs each hook as a shell command in the workspace root.

    Injects ``LITEHIVE_TASK_ID`` / ``LITEHIVE_STAGE`` / ``LITEHIVE_WORKSPACE``
    into the environment so hooks can key off the task under execution. On
    ``subprocess.TimeoutExpired`` the result is reported as ``ok=False`` with
    a timeout marker in the output — the node then emits ``Reject`` via the
    usual path.
    """

    def __init__(self, workspace_root: Path, *, extra_env: dict[str, str] | None = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.extra_env = dict(extra_env or {})

    def run(self, spec: HookSpec, state: TaskState) -> HookResult:
        env = {
            **os.environ,
            **self.extra_env,
            "LITEHIVE_TASK_ID": state.task_id,
            "LITEHIVE_STAGE": state.stage,
            "LITEHIVE_WORKSPACE": str(self.workspace_root),
        }
        try:
            proc = subprocess.run(
                spec.command,
                shell=True,
                cwd=str(self.workspace_root),
                timeout=spec.timeout_seconds,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = f"[timeout after {spec.timeout_seconds}s]\n{exc.stdout or ''}\n{exc.stderr or ''}"
            return HookResult(spec=spec, ok=False, output=output.strip())
        except FileNotFoundError as exc:
            return HookResult(spec=spec, ok=False, output=f"[hook binary missing] {exc}")

        output = (proc.stdout or "") + (proc.stderr or "")
        return HookResult(spec=spec, ok=proc.returncode == 0, output=output.strip())


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
