"""Backward-compatible shim for subagent helpers."""

from litehive.agents._continuation import extract_engine_continuation
from litehive.agents._models import (
    EngineFailure,
    SubagentInactivityTimeout,
    SubagentResult,
)
from litehive.agents._parsing import stage_report_from_subagent
from litehive.agents._prompts import intake_prompt, stage_prompt
from litehive.subagents._manager import SubagentManager

__all__ = [
    "EngineFailure",
    "SubagentInactivityTimeout",
    "SubagentManager",
    "SubagentResult",
    "extract_engine_continuation",
    "intake_prompt",
    "stage_prompt",
    "stage_report_from_subagent",
]
