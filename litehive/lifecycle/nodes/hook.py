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
    """Configured hook command for a state-machine node.

    Loaded from workspace config and passed to ``HookRunner.run``; the optional
    ``description`` and ``instructions_on_failure`` are surfaced verbatim in the
    Reject reason so the agent gets actionable feedback when a hook fails.
    """

    command: str
    timeout_seconds: float = 60
    description: str | None = None
    instructions_on_failure: str | None = None


class HookRunner(Protocol):
    def run(self, spec: HookSpec, state: TaskState) -> subprocess.CompletedProcess[str] | None:
        """
        Execute the hook and report success or failure.

        ``None`` is the pass signal — ``HookNode`` checks for it
        explicitly. A failed ``CompletedProcess`` carries the exit
        code, stdout, and stderr the rejection feedback will quote, so
        the agent prompt sees the actual hook output.
        """
        ...


def _failed_process(command: str, code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """
    Build a synthetic ``CompletedProcess`` for non-execution failures.

    Timeouts and missing-binary errors do not return a real
    ``CompletedProcess`` from ``subprocess.run``, but the rest of the
    runner expects one shape; faking it here lets ``HookNode`` treat
    every failure path the same way.
    """
    return subprocess.CompletedProcess(command, code, stdout, stderr)


class SubprocessHookRunner(HookRunner):
    """Production HookRunner that shells out under ``workspace_root`` with task identity in the env.

    ``execution_root_resolver`` lets the runner aim a hook at the per-task
    worktree instead of the main checkout, which is how implementing/testing
    stages run hooks against the agent's own branch.
    """

    def __init__(
        self,
        workspace_root: Path,
        execution_root_resolver: Callable[[TaskState], Path] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        """
        Bind the runner to a workspace plus optional per-task cwd
        resolver and shared env.

        The resolver is what lets implementing/testing hooks run
        against the per-task worktree instead of the main checkout —
        without it, a hook that checked git status would see the
        wrong tree entirely.
        """
        self.workspace_root = Path(workspace_root)
        self.execution_root_resolver = execution_root_resolver
        self.extra_env = dict(extra_env or {})

    def run(self, spec: HookSpec, state: TaskState) -> subprocess.CompletedProcess[str] | None:
        """
        Execute one ``HookSpec`` under the resolved cwd.

        Timeouts and missing-binary failures are normalized into the
        same ``CompletedProcess`` shape the node expects; agent identity
        and stage are exported into the hook's env so a hook can
        introspect which task it is gating without arguments.
        """
        if self.execution_root_resolver is None:
            execution_root = self.workspace_root
        else:
            execution_root = Path(self.execution_root_resolver(state))
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
            stdout_text = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr_text = exc.stderr if isinstance(exc.stderr, str) else ""
            return _failed_process(
                spec.command,
                124,
                stdout=stdout_text.strip(),
                stderr=f"[timeout after {spec.timeout_seconds}s]\n{stderr_text.strip()}".strip(),
            )
        except FileNotFoundError as exc:
            return _failed_process(spec.command, 127, stderr=f"[hook binary missing] {exc}")
        if proc.returncode == 0:
            return None
        return _failed_process(
            spec.command, proc.returncode, stdout=(proc.stdout or "").strip(), stderr=(proc.stderr or "").strip()
        )


class HookNode(Node):
    """State-machine node that runs configured hooks for a stage and emits ``HookOk`` or ``Reject``.

    Built once per hook-bearing stage by the lifecycle factory; the state
    machine treats it like any other node so hook failures route through the
    same Reject path as agent rejections.
    """

    node_type = NodeType.HOOK

    def __init__(self, name: PipelineState, hooks: list[HookSpec], runner: HookRunner) -> None:
        """Bind the node to its stage label, the ordered list of hooks, and the runner that executes them — one ``HookNode`` per hook-bearing phase."""
        self.name = name
        self.hooks = hooks
        self.runner = runner

    def run(self, state: TaskState) -> Event:
        """Run hooks in order and short-circuit on the first failure; passing all hooks yields ``HookOk`` so the rule table can advance to the next phase."""
        for spec in self.hooks:
            result = self.runner.run(spec, state)
            if result is not None:
                return _reject(self.name, spec, result, state)
        return HookOk()


def _reject(point: PipelineState, spec: HookSpec, result: subprocess.CompletedProcess[str], state: TaskState) -> Reject:
    """
    Build the Reject event for a failed hook.

    The fingerprint (``point|command|description``) is what lets the
    same-hook circuit breaker detect repeated failures of the same
    hook and route to recovery; without it, a flaky hook that keeps
    failing identically would just retry forever.
    """
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
    if previous is not None and previous.fingerprint == hook["fingerprint"]:
        same_hook_rejects = state.consecutive_same_hook_rejects + 1
    else:
        same_hook_rejects = 1
    reason = f"Runner hook rejected at `{point}`: `{spec.command}`."
    if description:
        reason += f" Description: {description}"
    return Reject(
        source="hook",
        reason=reason,
        metadata={"hook": hook, "warnings": [warning], "consecutive_same_hook_rejects": same_hook_rejects},
    )
