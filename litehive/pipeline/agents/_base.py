from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..nodes.agent import AgentNode, EngineSelector, SessionProvider
from ..persistence import TaskState
from ..types import NodeName
from ._global import default_startup_guidance


@dataclass
class PromptContext:
    """Workspace-level context the runner provides to agents at construction.

    - ``workspace_root``: repo root; used to read per-project overlays at
      ``.litehive/agents/{role}.md`` and ``.litehive/agents/all.md``.
    - ``startup_guidance``: extra bullets per role, merged on top of the
      built-in ``DEFAULT_STARTUP_GUIDANCE``. Usually comes from workspace
      config.
    - ``profile_overlay``: optional process-profile YAML (generic, codehive,
      django, …) that can add per-stage instruction blocks.
    """

    workspace_root: Path | None = None
    startup_guidance: dict[str, list[str]] = field(default_factory=dict)
    profile_overlay: dict[str, Any] | None = None


def _bulletize(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


class RoleAgent(AgentNode):
    """Base for stage-bound agents.

    Subclasses set three class attributes:

    - ``NODE_NAME`` — pipeline node the agent implements (``"grooming"``, …).
    - ``ROLE`` — logical agent role (``"planner"``, ``"swe"``, …).
    - ``INSTRUCTIONS`` — role-specific bullet block (baseline).

    ``build_prompt`` composes instructions from four layers:

    1. Role-specific (the ``INSTRUCTIONS`` class constant).
    2. Built-in startup guidance for this role, optionally extended by
       workspace config.
    3. A per-workspace ``.litehive/agents/{role}.md`` override. If present
       it REPLACES layer 2 for that role.
    4. An ``"all"`` layer applied to every role, with the same md-override
       behavior.
    5. An optional process-profile overlay keyed by stage name.
    """

    NODE_NAME: NodeName = ""
    ROLE: str = ""
    INSTRUCTIONS: str = ""

    def __init__(
        self,
        selector: EngineSelector,
        session_provider: SessionProvider,
        *,
        prompt_context: PromptContext | None = None,
        retry_budget: int = 3,
        grace_period_seconds: int | None = None,
    ) -> None:
        if not self.NODE_NAME or not self.ROLE:
            raise TypeError(
                f"{type(self).__name__} must set NODE_NAME and ROLE class attributes"
            )
        super().__init__(
            name=self.NODE_NAME,
            selector=selector,
            session_provider=session_provider,
            retry_budget=retry_budget,
            grace_period_seconds=grace_period_seconds,
        )
        self.prompt_context = prompt_context or PromptContext()

    def build_prompt(self, state: TaskState) -> dict[str, Any]:
        last_rejection = state.last_rejection_by_stage.get(self.NODE_NAME)
        return {
            "role": self.ROLE,
            "stage": self.NODE_NAME,
            "task_id": state.task_id,
            "pipeline_mode": state.pipeline_mode.value,
            "stage_retry": state.stage_retry.get(self.NODE_NAME, 0),
            "instruction_layers": self._assemble_instruction_layers(),
            "last_rejection": (
                {
                    "source": last_rejection.source,
                    "reason": last_rejection.reason,
                    "raised_at_phase": last_rejection.raised_at_phase,
                }
                if last_rejection is not None
                else None
            ),
            "thread": state.failure_context.get("thread", []),
        }

    def _assemble_instruction_layers(self) -> list[tuple[str, str]]:
        layers: list[tuple[str, str]] = [("role", self.INSTRUCTIONS.strip())]

        for key in ("all", self.ROLE):
            md = self._load_overlay_md(key)
            if md is not None:
                layers.append((f"{key}:md", md))
                continue
            bullets = self._startup_guidance_for(key)
            if bullets:
                layers.append((f"{key}:startup", _bulletize(bullets)))

        profile = self.prompt_context.profile_overlay
        if profile:
            stage_block = profile.get("stages", {}).get(self.NODE_NAME)
            if stage_block:
                layers.append(("profile", stage_block))

        return layers

    def _startup_guidance_for(self, key: str) -> list[str]:
        merged = list(default_startup_guidance().get(key, []))
        merged.extend(self.prompt_context.startup_guidance.get(key, []))
        return merged

    def _load_overlay_md(self, key: str) -> str | None:
        root = self.prompt_context.workspace_root
        if root is None:
            return None
        md_path = root / ".litehive" / "agents" / f"{key}.md"
        if not md_path.is_file():
            return None
        text = md_path.read_text(encoding="utf-8").strip()
        return text or None
