import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from litehive.domain.common import PipelineState, cap_feedback

from ..events import Event, HookOk, Reject
from ..persistence import TaskState
from ..types import NodeType
from .base import Node

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HookSpec:
    command: str
    timeout_seconds: float = 60
    description: str | None = None
    instructions_on_failure: str | None = None


class HookRunner(Protocol):
    def run(self, spec: HookSpec, state: TaskState) -> subprocess.CompletedProcess[str] | None: ...


def _failed_process(command: str, code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, stdout, stderr)


class SubprocessHookRunner(HookRunner):
    def __init__(
        self,
        workspace_root: Path,
        execution_root_resolver: Callable[[TaskState], Path] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.execution_root_resolver = execution_root_resolver
        self.extra_env = dict(extra_env or {})

    def run(self, spec: HookSpec, state: TaskState) -> subprocess.CompletedProcess[str] | None:
        execution_root = (
            self.workspace_root if self.execution_root_resolver is None else Path(self.execution_root_resolver(state))
        )
        env = {
            **os.environ,
            **self.extra_env,
            "LITEHIVE_TASK_ID": state.task_id,
            "LITEHIVE_STAGE": state.stage,
            "LITEHIVE_WORKSPACE": str(execution_root),
        }
        try:
            proc = subprocess.run(
                spec.command,
                shell=True,
                cwd=str(execution_root),
                timeout=spec.timeout_seconds,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _failed_process(
                spec.command,
                124,
                stdout=(exc.stdout or "").strip(),
                stderr=f"[timeout after {spec.timeout_seconds}s]\n{(exc.stderr or '').strip()}".strip(),
            )
        except FileNotFoundError as exc:
            return _failed_process(spec.command, 127, stderr=f"[hook binary missing] {exc}")
        if proc.returncode == 0:
            return None
        return _failed_process(
            spec.command, proc.returncode, stdout=(proc.stdout or "").strip(), stderr=(proc.stderr or "").strip()
        )


class HookNode(Node):
    node_type = NodeType.HOOK

    def __init__(self, name: PipelineState, hooks: list[HookSpec], runner: HookRunner) -> None:
        self.name = name
        self.hooks = hooks
        self.runner = runner

    def run(self, state: TaskState) -> Event:
        for spec in self.hooks:
            result = self.runner.run(spec, state)
            if result is not None:
                return _reject(self.name, spec, result, state)
        return HookOk()


def _reject(point: PipelineState, spec: HookSpec, result: subprocess.CompletedProcess[str], state: TaskState) -> Reject:
    description = (spec.description or "").strip()
    hook = {
        "point": point,
        "command": spec.command,
        "description": description,
        "fingerprint": f"{point}|{spec.command}|{description}",
    }
    parts = [f"Runner hook warning at `{point}`: `{spec.command}` exited with {result.returncode}."]
    if description:
        parts.append(f"Description: {description}")
    if result.stdout:
        parts.append(f"stdout:\n{cap_feedback(result.stdout)}")
    if result.stderr:
        parts.append(f"stderr:\n{cap_feedback(result.stderr)}")
    if spec.instructions_on_failure:
        parts.append(f"Instructions on failure: {spec.instructions_on_failure}")
    warning = "\n".join(parts)
    log.warning("%s", warning)
    previous = state.last_hook_reject_fingerprint
    same_hook_rejects = (
        state.consecutive_same_hook_rejects + 1
        if previous is not None and previous.fingerprint == hook["fingerprint"]
        else 1
    )
    reason = f"Runner hook rejected at `{point}`: `{spec.command}`."
    if description:
        reason += f" Description: {description}"
    return Reject(
        source="hook",
        reason=reason,
        metadata={"hook": hook, "warnings": [warning], "consecutive_same_hook_rejects": same_hook_rejects},
    )
