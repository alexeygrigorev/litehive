"""Typed Litehive process environment."""

import os
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class LitehiveEnvironment:
    """
    Values Litehive reads from the process environment at CLI boundaries.

    Captures the ``LITEHIVE_*`` variables that identify the running
    agent, stage, task, and workspace. Collected once at process entry
    so downstream code does not read ``os.environ`` directly.

    Attributes:
        agent_role: The role the current agent is playing (e.g. ``swe``,
            ``reviewer``), or ``None`` when not running inside an agent.
        agent_stage: The pipeline stage the agent was spawned for.
        subagent_id: Unique identifier for the current subagent process.
        task_id: The task this process is executing, if any.
        workspace_root: Absolute path to the workspace root directory.
    """

    agent_role: str | None
    agent_stage: str | None
    subagent_id: str | None
    task_id: str | None
    workspace_root: str | None

    @classmethod
    def from_process(cls) -> "LitehiveEnvironment":
        """
        Read the Litehive environment from the current process.

        Convenience entry point for production code; tests should prefer
        ``from_mapping`` to avoid depending on the real environment.
        """
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "LitehiveEnvironment":
        """
        Build an environment from an arbitrary string mapping.

        Accepts ``os.environ`` or a test-supplied dict. Each value is
        normalised so blank or whitespace-only strings become ``None``.
        """
        return cls(
            agent_role=_normalized_optional(values.get("LITEHIVE_AGENT_ROLE")),
            agent_stage=_normalized_optional(values.get("LITEHIVE_STAGE")),
            subagent_id=_normalized_optional(values.get("LITEHIVE_SUBAGENT_ID")),
            task_id=_normalized_optional(values.get("LITEHIVE_TASK_ID")),
            workspace_root=_normalized_optional(values.get("LITEHIVE_WORKSPACE_ROOT")),
        )


def _normalized_optional(value: str | None) -> str | None:
    """
    Strip whitespace and return ``None`` for empty or blank strings.

    Shared normaliser for all ``LITEHIVE_*`` env reads so operators can
    write ``LITEHIVE_TASK_ID=""`` without producing a spurious id.
    """
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
