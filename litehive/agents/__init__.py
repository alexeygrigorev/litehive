"""Unified agent package: adapters, prompts, parsing, sessions, and manager."""

from importlib import import_module
from typing import TYPE_CHECKING

from heru.adapters import (
    ClaudeCLIAdapter,
    CodexCLIAdapter,
    CopilotCLIAdapter,
    EngineError,
    GeminiCLIAdapter,
    GozCLIAdapter,
    OpenCodeAdapter,
    RetryableExecutionFailure,
    _ENGINE_LIMIT_PATTERNS,
    _EXECUTION_INTERRUPTION_PATTERNS,
    _RETRYABLE_EXECUTION_PATTERNS,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from heru.base import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
    StreamEventAdapter,
    extract_jsonl_errors,
    extract_jsonl_messages,
    extract_live_timeline,
    extract_stream_errors,
    extract_stream_transcript,
    iter_jsonl_payloads,
    parse_stage_report_text,
)
from heru.types import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
    LiveTimeline,
    RuntimeEngineContinuation,
)

from heru import (
    ENGINE_ADAPTER_TYPES,
    ENGINE_CHOICES,
    ENGINE_REGISTRY,
    extract_engine_continuation,
    extract_engine_timeline,
    get_engine,
    get_stream_event_adapter,
)

if TYPE_CHECKING:
    from litehive.agents.manager import SubagentManager
    from litehive.agents.models import EngineFailure, SubagentInactivityTimeout, SubagentResult
    from litehive.agents.parsing import stage_report_from_subagent
    from litehive.agents.prompts import intake_prompt, stage_prompt


_LAZY_EXPORTS = {
    "SubagentManager": ("litehive.agents.manager", "SubagentManager"),
    "EngineFailure": ("litehive.agents.models", "EngineFailure"),
    "SubagentInactivityTimeout": ("litehive.agents.models", "SubagentInactivityTimeout"),
    "SubagentResult": ("litehive.agents.models", "SubagentResult"),
    "stage_report_from_subagent": ("litehive.agents.parsing", "stage_report_from_subagent"),
    "intake_prompt": ("litehive.agents.prompts", "intake_prompt"),
    "stage_prompt": ("litehive.agents.prompts", "stage_prompt"),
}


def __getattr__(name: str) -> object:
    try:
        module_name, attr_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value

__all__ = [
    "AdapterCapabilities",
    "CLIExecutionResult",
    "ClaudeCLIAdapter",
    "CodexCLIAdapter",
    "CopilotCLIAdapter",
    "ENGINE_CHOICES",
    "ENGINE_REGISTRY",
    "ENGINE_ADAPTER_TYPES",
    "EngineError",
    "EngineFailure",
    "EngineUsageObservation",
    "EngineUsageWindow",
    "ExternalCLIAdapter",
    "GeminiCLIAdapter",
    "GozCLIAdapter",
    "LiveEvent",
    "LiveTimeline",
    "OpenCodeAdapter",
    "RetryableExecutionFailure",
    "RuntimeEngineContinuation",
    "StreamEventAdapter",
    "SubagentInactivityTimeout",
    "SubagentManager",
    "SubagentResult",
    "_ENGINE_LIMIT_PATTERNS",
    "_EXECUTION_INTERRUPTION_PATTERNS",
    "_RETRYABLE_EXECUTION_PATTERNS",
    "classify_execution_interruption",
    "classify_execution_limit",
    "classify_retryable_execution_failure",
    "extract_engine_continuation",
    "extract_engine_timeline",
    "extract_jsonl_errors",
    "extract_jsonl_messages",
    "extract_live_timeline",
    "extract_stream_errors",
    "extract_stream_transcript",
    "get_engine",
    "get_stream_event_adapter",
    "intake_prompt",
    "iter_jsonl_payloads",
    "parse_stage_report_text",
    "stage_prompt",
    "stage_report_from_subagent",
]
