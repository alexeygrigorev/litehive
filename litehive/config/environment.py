"""Typed Litehive process environment."""

import os
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class LitehiveEnvironment:
    """
    Values Litehive reads from the process environment at CLI boundaries.
    """

    agent_role: str | None
    agent_stage: str | None
    subagent_id: str | None
    task_id: str | None
    workspace_root: str | None

    @classmethod
    def from_process(cls) -> "LitehiveEnvironment":
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "LitehiveEnvironment":
        return cls(
            agent_role=_normalized_optional(values.get("LITEHIVE_AGENT_ROLE")),
            agent_stage=_normalized_optional(values.get("LITEHIVE_STAGE")),
            subagent_id=_normalized_optional(values.get("LITEHIVE_SUBAGENT_ID")),
            task_id=_normalized_optional(values.get("LITEHIVE_TASK_ID")),
            workspace_root=_normalized_optional(values.get("LITEHIVE_WORKSPACE_ROOT")),
        )


def _normalized_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
