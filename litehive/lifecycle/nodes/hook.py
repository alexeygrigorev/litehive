import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from litehive.domain.common import cap_feedback

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
    description: str | None = None
    instructions_on_failure: str | None = None


@dataclass
class HookResult:
    spec: HookSpec
    ok: bool
    output: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


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
            stdout = exc.stdout or ""
            stderr = f"[timeout after {spec.timeout_seconds}s]\n{exc.stderr or ''}".strip()
            output = _combine_output(stdout, stderr)
            return HookResult(
                spec=spec,
                ok=False,
                output=output,
                exit_code=124,
                stdout=stdout.strip(),
                stderr=stderr,
            )
        except FileNotFoundError as exc:
            stderr = f"[hook binary missing] {exc}"
            return HookResult(
                spec=spec,
                ok=False,
                output=stderr,
                exit_code=127,
                stderr=stderr,
            )

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        output = _combine_output(stdout, stderr)
        return HookResult(
            spec=spec,
            ok=proc.returncode == 0,
            output=output,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )


class HookNode(Node):
    node_type = NodeType.HOOK

    def __init__(
        self,
        name: NodeName,
        hooks: list[HookSpec],
        runner: HookRunner,
        *,
        execution_mode: ExecutionMode = ExecutionMode.RUN_ALL,
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
            if self.execution_mode == ExecutionMode.FAIL_FAST and not result.ok and spec.reject_on_failure:
                break
        rejecting = [r for r in results if not r.ok and r.spec.reject_on_failure]
        if rejecting:
            primary = rejecting[0]
            hook = self._fingerprint(self.name, primary.spec)
            same_as_last = (
                state.last_hook_reject_fingerprint is not None
                and state.last_hook_reject_fingerprint.fingerprint == hook["fingerprint"]
            )
            consecutive = state.consecutive_same_hook_rejects + 1 if same_as_last else 1
            hook_results = [self._result_payload(result) for result in rejecting]
            return Reject(
                source="hook",
                reason=cap_feedback(self._feedback(self.name, hook_results)),
                metadata={
                    "hook": hook,
                    "hook_results": hook_results,
                    "consecutive_same_hook_rejects": consecutive,
                },
            )
        return HookOk()

    @staticmethod
    def _feedback(point: NodeName, results: list[dict[str, str | int | bool | None]]) -> str:
        lines = [f"Runner hook failure at `{point}`:"]
        for index, result in enumerate(results, start=1):
            command = result.get("command") or "(unknown command)"
            lines.append(f"{index}. Command: {command}")
            description = result.get("description")
            if description:
                lines.append(f"   Description: {description}")
            exit_code = result.get("exit_code")
            lines.append(f"   Exit code: {exit_code if exit_code is not None else 'unknown'}")
            lines.append(f"   stdout:\n{result.get('stdout') or '(empty)'}")
            lines.append(f"   stderr:\n{result.get('stderr') or '(empty)'}")
            instructions = result.get("instructions_on_failure")
            if instructions:
                lines.append(f"   Instructions on failure: {instructions}")
        return "\n".join(lines)

    @staticmethod
    def _fingerprint(point: NodeName, spec: HookSpec) -> dict[str, str]:
        description = spec.description or ""
        return {
            "point": point,
            "command": spec.command,
            "description": description,
            "fingerprint": f"{point}|{spec.command}|{description}",
        }

    @staticmethod
    def _result_payload(result: HookResult) -> dict[str, str | int | bool | None]:
        return {
            "command": result.spec.command,
            "description": result.spec.description or "",
            "exit_code": result.exit_code,
            "stdout": cap_feedback(result.stdout),
            "stderr": cap_feedback(result.stderr),
            "instructions_on_failure": result.spec.instructions_on_failure or "",
        }


def _combine_output(stdout: str, stderr: str) -> str:
    return "\n".join(part for part in (stdout.strip(), stderr.strip()) if part).strip()
