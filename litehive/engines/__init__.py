"""External CLI engine adapters."""

# ruff: noqa: F401

from litehive.engines.adapters import (
    ClaudeCLIAdapter,
    CodexCLIAdapter,
    CopilotCLIAdapter,
    EngineError,
    GeminiCLIAdapter,
    GozCLIAdapter,
    OpenCodeAdapter,
    RetryableExecutionFailure,
    _CLAUDE_STREAM_EVENT_ADAPTER,
    _COPILOT_STREAM_EVENT_ADAPTER,
    _ENGINE_LIMIT_PATTERNS,
    _EXECUTION_INTERRUPTION_PATTERNS,
    _OPENCODE_STRIPPED_ENV_VARS,
    _RETRYABLE_EXECUTION_PATTERNS,
    _claude_live_events,
    _codex_live_events,
    _copilot_live_events,
    _gemini_live_events,
    _goz_live_events,
    _opencode_live_events,
    _unwrap_claude_stream_event,
    classify_execution_interruption,
    classify_execution_limit,
    classify_retryable_execution_failure,
)
from litehive.engines.base import (
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
from litehive.models import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
    LiveTimeline,
    RuntimeEngineContinuation,
)


ENGINE_REGISTRY: dict[str, ExternalCLIAdapter] = {
    "codex": CodexCLIAdapter(
        name="codex",
        binary="codex",
        capabilities=AdapterCapabilities(
            supports_model_override=False,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
    "opencode": OpenCodeAdapter(
        name="opencode",
        binary="opencode",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=True,
            transcript_format="jsonl",
        ),
        stripped_env_vars=_OPENCODE_STRIPPED_ENV_VARS,
    ),
    "goz": GozCLIAdapter(
        name="goz",
        binary="goz",
        capabilities=AdapterCapabilities(
            supports_model_override=False,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
    "gemini": GeminiCLIAdapter(
        name="gemini",
        binary="gemini",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
    "copilot": CopilotCLIAdapter(
        name="copilot",
        binary="copilot",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
    "claude": ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
}

ENGINE_CHOICES = sorted(ENGINE_REGISTRY.keys())


def get_engine(name: str) -> ExternalCLIAdapter:
    try:
        return ENGINE_REGISTRY[name]
    except KeyError as exc:
        raise EngineError(f"Unknown engine '{name}'") from exc


ENGINE_STREAM_EVENT_ADAPTERS: dict[str, StreamEventAdapter] = {
    "codex": StreamEventAdapter(
        live_events=_codex_live_events,
        unwrap_event=_unwrap_claude_stream_event,
    ),
    "opencode": StreamEventAdapter(
        live_events=_opencode_live_events,
    ),
    "goz": StreamEventAdapter(
        live_events=_goz_live_events,
    ),
    "gemini": StreamEventAdapter(
        live_events=_gemini_live_events,
    ),
    "copilot": _COPILOT_STREAM_EVENT_ADAPTER,
    "claude": _CLAUDE_STREAM_EVENT_ADAPTER,
}


def get_stream_event_adapter(engine_name: str) -> StreamEventAdapter | None:
    return ENGINE_STREAM_EVENT_ADAPTERS.get(engine_name)


def extract_engine_timeline(
    engine_name: str,
    stdout: str,
    *,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> LiveTimeline | None:
    if not stdout.strip():
        return None
    adapter = get_stream_event_adapter(engine_name)
    timeline = extract_live_timeline(stdout, engine=engine_name, adapter=adapter)
    if not timeline.events:
        return None
    if task_id is not None:
        timeline.task_id = task_id
    if subagent_id is not None:
        timeline.subagent_id = subagent_id
    return timeline


def extract_engine_continuation(
    engine_name: str, execution: CLIExecutionResult | None
) -> RuntimeEngineContinuation | None:
    if execution is None or not execution.stdout.strip():
        return None

    if engine_name == "codex":
        for payload in iter_jsonl_payloads(execution.stdout):
            if payload.get("type") != "thread.started":
                continue
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return RuntimeEngineContinuation(thread_id=thread_id)
        return None

    if engine_name == "opencode":
        for payload in iter_jsonl_payloads(execution.stdout):
            session_id = payload.get("sessionID")
            if isinstance(session_id, str) and session_id:
                return RuntimeEngineContinuation(session_id=session_id)
        return None

    if engine_name == "gemini":
        for payload in iter_jsonl_payloads(execution.stdout):
            if payload.get("type") != "init":
                continue
            session_id = payload.get("session_id")
            model = payload.get("model")
            metadata: dict[str, str | int | bool | None] = {}
            if isinstance(model, str) and model:
                metadata["model"] = model
            if isinstance(session_id, str) and session_id:
                return RuntimeEngineContinuation(session_id=session_id, metadata=metadata)
        return None

    if engine_name == "claude":
        for payload in iter_jsonl_payloads(execution.stdout):
            if payload.get("type") == "system" and payload.get("subtype") == "init":
                session_id = payload.get("session_id")
                if isinstance(session_id, str) and session_id:
                    return RuntimeEngineContinuation(session_id=session_id)
        return None

    if engine_name == "copilot":
        return None

    return None
