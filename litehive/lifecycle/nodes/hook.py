import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from litehive.domain.common import cap_feedback

from ..events import Event, HookOk, Reject
from ..persistence import TaskState
from ..types import NodeName, NodeType
from .base import Node

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HookSpec:
    command: str
    timeout_seconds: float = 60
    description: str | None = None
    instructions_on_failure: str | None = None


@dataclass(frozen=True)
class HookResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class HookRunner:
    def run(self, spec: HookSpec, state: TaskState) -> HookResult | None:
        raise NotImplementedError


ExecutionRootResolver = Callable[[TaskState], Path]


class SubprocessHookRunner(HookRunner):
    def __init__(
        self,
        workspace_root: Path,
        *,
        execution_root_resolver: ExecutionRootResolver | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.execution_root_resolver = execution_root_resolver
        self.extra_env = dict(extra_env or {})

    def run(self, spec: HookSpec, state: TaskState) -> HookResult | None:
        execution_root = self._execution_root(state)
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
            return HookResult(
                exit_code=124,
                stdout=(exc.stdout or "").strip(),
                stderr=f"[timeout after {spec.timeout_seconds}s]\n{(exc.stderr or '').strip()}".strip(),
            )
        except FileNotFoundError as exc:
            return HookResult(exit_code=127, stderr=f"[hook binary missing] {exc}")
        if proc.returncode == 0:
            return None
        return HookResult(
            exit_code=proc.returncode,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
        )

    def _execution_root(self, state: TaskState) -> Path:
        if self.execution_root_resolver is None:
            return self.workspace_root
        return Path(self.execution_root_resolver(state))


class HookNode(Node):
    node_type = NodeType.HOOK

    def __init__(self, name: NodeName, hooks: list[HookSpec], runner: HookRunner) -> None:
        self.name = name
        self.hooks = hooks
        self.runner = runner

    def run(self, state: TaskState) -> Event:
        failures: list[dict[str, object]] = []
        for spec in self.hooks:
            result = self.runner.run(spec, state)
            if result is None:
                continue
            warning = _format_warning(self.name, spec, result)
            log.warning("%s", warning)
            failures.append(
                {
                    "hook": _hook_payload(self.name, spec),
                    "warning": warning,
                    "exit_code": result.exit_code,
                }
            )
        if not failures:
            return HookOk(warnings=[])
        primary_failure = failures[0]
        hook = primary_failure["hook"]
        warnings = [str(item["warning"]) for item in failures]
        metadata = {
            "hook": hook,
            "warnings": warnings,
            "failed_hooks": [
                {
                    "hook": item["hook"],
                    "exit_code": item["exit_code"],
                    "warning": item["warning"],
                }
                for item in failures
            ],
            "consecutive_same_hook_rejects": _prospective_same_hook_reject_count(state, str(hook["fingerprint"])),
        }
        return Reject(
            source="hook",
            reason=_format_reject_reason(self.name, hook),
            metadata=metadata,
        )


def _hook_payload(point: NodeName, spec: HookSpec) -> dict[str, str]:
    description = (spec.description or "").strip()
    return {
        "point": point,
        "command": spec.command,
        "description": description,
        "fingerprint": f"{point}|{spec.command}|{description}",
    }


def _prospective_same_hook_reject_count(state: TaskState, fingerprint: str) -> int:
    previous = state.last_hook_reject_fingerprint
    if previous is not None and previous.fingerprint == fingerprint:
        return state.consecutive_same_hook_rejects + 1
    return 1


def _format_reject_reason(point: NodeName, hook: dict[str, str]) -> str:
    command = hook["command"]
    description = hook.get("description", "")
    summary = f"Runner hook rejected at `{point}`: `{command}`."
    if description:
        summary += f" Description: {description}"
    return summary


def _format_warning(point: NodeName, spec: HookSpec, result: HookResult) -> str:
    lines = [f"Runner hook warning at `{point}`: `{spec.command}` exited with {result.exit_code}."]
    if spec.description:
        lines.append(f"Description: {spec.description}")
    if result.stdout:
        lines.append(f"stdout:\n{cap_feedback(result.stdout)}")
    if result.stderr:
        lines.append(f"stderr:\n{cap_feedback(result.stderr)}")
    if spec.instructions_on_failure:
        lines.append(f"Instructions on failure: {spec.instructions_on_failure}")
    return "\n".join(lines)
