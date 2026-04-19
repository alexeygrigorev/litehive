import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from litehive.domain.common import cap_feedback
from ..events import HookOk
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
class SubprocessHookRunner(HookRunner):
    def __init__(self, workspace_root: Path, *, extra_env: dict[str, str] | None = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.extra_env = dict(extra_env or {})

    def run(self, spec: HookSpec, state: TaskState) -> HookResult | None:
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
class HookNode(Node):
    node_type = NodeType.HOOK

    def __init__(self, name: NodeName, hooks: list[HookSpec], runner: HookRunner) -> None:
        self.name = name
        self.hooks = hooks
        self.runner = runner

    def run(self, state: TaskState) -> HookOk:
        warnings: list[str] = []
        for spec in self.hooks:
            result = self.runner.run(spec, state)
            if result is None:
                continue
            warning = _format_warning(self.name, spec, result)
            log.warning("%s", warning)
            warnings.append(warning)
        return HookOk(warnings=warnings)
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
