"""Public package entrypoints for heru's stable adapter contract.

Callers may rely on the names re-exported here as the supported public
surface for engine lookup, adapter classes, execution records, and the
unified event models documented in the README API Contract section.
"""

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
    CLIInvocation,
    ExternalCLIAdapter,
    LATEST_CONTINUATION_SENTINEL,
    StreamEventAdapter,
    extract_jsonl_errors,
    extract_jsonl_messages,
    extract_live_timeline,
    extract_stream_errors,
    extract_stream_transcript,
    iter_jsonl_payloads,
)
from heru.types import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
    LiveTimeline,
    RuntimeEngineContinuation,
    UnifiedEvent,
)
from heru.unified_events import (
    UnifiedExecutionView,
    parse_unified_execution,
    render_execution_transcript,
)

ENGINE_ADAPTER_TYPES: tuple[type[ExternalCLIAdapter], ...] = (
    CodexCLIAdapter,
    OpenCodeAdapter,
    GozCLIAdapter,
    GeminiCLIAdapter,
    CopilotCLIAdapter,
    ClaudeCLIAdapter,
)

ENGINE_REGISTRY: dict[str, ExternalCLIAdapter] = {
    adapter.name: adapter for adapter in (adapter_type() for adapter_type in ENGINE_ADAPTER_TYPES)
}

ENGINE_CHOICES = sorted(ENGINE_REGISTRY.keys())


def get_engine(name: str) -> ExternalCLIAdapter:
    """Public engine lookup that resolves a stable engine name to its adapter."""
    try:
        return ENGINE_REGISTRY[name]
    except KeyError as exc:
        raise EngineError(f"Unknown engine '{name}'") from exc


def get_stream_event_adapter(engine_name: str) -> StreamEventAdapter | None:
    adapter = ENGINE_REGISTRY.get(engine_name)
    if adapter is None:
        return None
    return adapter.stream_event_adapter()


def extract_engine_timeline(
    engine_name: str,
    stdout: str,
    *,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> LiveTimeline | None:
    if not stdout.strip():
        return None
    unified = parse_unified_execution(stdout)
    if unified is not None:
        return unified.timeline(
            engine_name=engine_name,
            task_id=task_id,
            subagent_id=subagent_id,
        )
    adapter = ENGINE_REGISTRY.get(engine_name)
    timeline = extract_live_timeline(
        stdout,
        engine=engine_name,
        adapter=None if adapter is None else adapter.stream_event_adapter(),
        payloads=None if adapter is None else adapter._iter_live_native_payloads(stdout),
    )
    if not timeline.events:
        return None
    if task_id is not None:
        timeline.task_id = task_id
    if subagent_id is not None:
        timeline.subagent_id = subagent_id
    return timeline


def extract_engine_continuation_for_execution(
    engine_name: str, execution: CLIExecutionResult | None
) -> RuntimeEngineContinuation | None:
    if execution is None or not execution.stdout.strip():
        return None
    unified = parse_unified_execution(execution.stdout)
    if unified is not None:
        continuation = unified.continuation()
        if continuation is not None:
            return continuation
    adapter = ENGINE_REGISTRY.get(engine_name)
    if adapter is None:
        return None
    return adapter.extract_continuation(execution)


extract_engine_continuation = extract_engine_continuation_for_execution


def resolve_engine_resume_session_id(
    engine_name: str,
    continuation: RuntimeEngineContinuation | str | None,
    *,
    prefer_latest: bool = False,
) -> str | None:
    if isinstance(continuation, str):
        return continuation or None
    if continuation is not None and continuation.resume_id:
        return continuation.resume_id
    if not prefer_latest:
        return None
    adapter = ENGINE_REGISTRY.get(engine_name)
    if adapter is None or not adapter.supports_continue_latest():
        return None
    return LATEST_CONTINUATION_SENTINEL


__all__ = [
    "AdapterCapabilities",
    "CLIExecutionResult",
    "CLIInvocation",
    "ClaudeCLIAdapter",
    "CodexCLIAdapter",
    "CopilotCLIAdapter",
    "ENGINE_ADAPTER_TYPES",
    "ENGINE_CHOICES",
    "ENGINE_REGISTRY",
    "EngineError",
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
    "UnifiedEvent",
    "UnifiedExecutionView",
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
    "iter_jsonl_payloads",
    "parse_unified_execution",
    "render_execution_transcript",
    "resolve_engine_resume_session_id",
]
