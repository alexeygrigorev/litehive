"""Minimal in-process heru stub for test runs in heru-less checkouts.

This project normally depends on the sibling ``../heru`` source checkout.
Some CI/workspace configurations do not have that repo available, but we
still want Litehive's unit tests to run against stable adapter contracts and
shared record models. This module installs a narrow in-memory stub into
``sys.modules`` so tests can import ``heru`` without reaching outside the
repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import sys
import types
from typing import Any, Literal

from pydantic import BaseModel, Field


ENGINE_CHOICES = ("claude", "codex", "copilot", "gemini", "goz", "opencode")
EngineLimitKind = Literal["quota", "rate", "budget", "resource", "unknown"]
EngineMonitoringSource = Literal["local", "provider"]
LiveEventKind = Literal["message", "status", "tool_call", "tool_result", "error", "continuation", "usage"]
LiveEventRole = Literal["assistant", "tool", "system", "user"]
SubagentStatus = Literal["queued", "running", "completed", "failed", "interrupted", "cancelled", "blocked"]


class EngineUsageWindow(BaseModel):
    used: int | float | None = None
    limit: int | float | None = None
    remaining: int | float | None = None
    unit: str | None = None
    reset_at: str | None = None
    percent_remaining: float = 100.0
    used_percent: float = 0.0


class EngineUsageObservation(BaseModel):
    source: EngineMonitoringSource = "local"
    provider: str | None = None
    observed_at: str | None = None
    invocation_count: int = 1
    success: bool | None = None
    limit_reason: str | None = None
    limit_kind: EngineLimitKind | None = None
    usage: EngineUsageWindow | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class RuntimeEngineContinuation(BaseModel):
    session_id: str | None = None
    cursor: str | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @property
    def resume_id(self) -> str | None:
        return self.session_id


class ResourceLimitEvent(BaseModel):
    resource: str
    reason: str
    observed_signal: str | None = None
    exit_code: int | None = None
    memory_mb: int | None = None
    cpu_count: float | None = None
    process_limit: int | None = None


class SubagentRef(BaseModel):
    id: str
    role: str
    engine: str
    status: SubagentStatus
    path: str
    sandboxed: bool = False
    sandbox_summary: str = ""


class LiveEvent(BaseModel):
    kind: LiveEventKind
    role: LiveEventRole | None = None
    content: str | None = None
    error: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    continuation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UnifiedEvent(BaseModel):
    kind: LiveEventKind
    engine: str | None = None
    sequence: int | None = None
    timestamp: str | None = None
    role: LiveEventRole | None = None
    content: str | None = None
    error: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    continuation_id: str | None = None
    usage_delta: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiveTimeline(BaseModel):
    engine: str
    task_id: str | None = None
    subagent_id: str | None = None
    events: list[LiveEvent] = Field(default_factory=list)
    event_counts: dict[str, int] = Field(default_factory=dict)
    message_count: int = 0
    status_count: int = 0
    error_count: int = 0
    tool_call_count: int = 0
    tool_result_count: int = 0

    def recompute_counts(self) -> None:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        self.event_counts = counts
        self.message_count = counts.get("message", 0)
        self.status_count = counts.get("status", 0)
        self.error_count = counts.get("error", 0)
        self.tool_call_count = counts.get("tool_call", 0)
        self.tool_result_count = counts.get("tool_result", 0)


class UsageWindow(BaseModel):
    percent_remaining: float = 100.0
    used_percent: float = 0.0
    reset_at: str | None = None


class UsageStatus(BaseModel):
    error: str | None = None
    limit_reached: bool = False
    short_term: UsageWindow = Field(default_factory=UsageWindow)
    long_term: UsageWindow = Field(default_factory=UsageWindow)


@dataclass(frozen=True, slots=True)
class CLIInvocation:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class CLIExecutionResult:
    adapter: str
    argv: tuple[str, ...]
    cwd: Path
    exit_code: int
    stdout: str
    stderr: str
    pid: int | None = None

    @property
    def transcript(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout}\n\n[stderr]\n{self.stderr}"
        return self.stdout or self.stderr


class AdapterCapabilities(BaseModel):
    available: bool = True
    supports_model_override: bool = False
    transcript_format: str = "text"
    strips_environment: bool = False


class EngineError(RuntimeError):
    """Stub engine execution failure."""


class RetryableExecutionFailure(RuntimeError):
    """Stub retryable failure classification."""

    def __init__(self, reason: str, *, classification: str | None = None, kind: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.classification = classification
        self.kind = kind


class ExternalCLIAdapter:
    def __init__(
        self,
        *,
        name: str,
        binary: str,
        capabilities: AdapterCapabilities | None = None,
    ) -> None:
        self.name = name
        self.binary = binary
        self.capabilities = capabilities or AdapterCapabilities()

    def is_available(self) -> bool:
        return True

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
    ) -> list[str]:
        del cwd, model, max_turns
        return [self.binary, prompt]

    def build_invocation(
        self,
        prompt: str,
        *,
        cwd: Path,
        model: str | None = None,
        max_turns: int | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> CLIInvocation:
        return CLIInvocation(
            argv=tuple(self.build_command(prompt, cwd, model=model, max_turns=max_turns)),
            cwd=cwd,
            env=extra_env,
        )

    def run(self, prompt: str, cwd: Path, model: str | None = None, **_: Any) -> CLIExecutionResult:
        return CLIExecutionResult(
            adapter=self.name,
            argv=tuple(self.build_command(prompt, cwd, model=model)),
            cwd=cwd,
            exit_code=0,
            stdout="",
            stderr="",
        )

    def run_live(self, prompt: str, cwd: Path, model: str | None = None, **kwargs: Any) -> CLIExecutionResult:
        return self.run(prompt, cwd, model=model, **kwargs)

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return execution.transcript

    def extract_usage_observation(self, execution: CLIExecutionResult) -> EngineUsageObservation | None:
        return _extract_usage_observation(self.name, execution)


def _iter_json_objects(text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            objects.append(payload)
    return objects


def iter_jsonl_payloads(text: str):
    yield from _iter_json_objects(text)


def _extract_usage_observation(engine_name: str, execution: CLIExecutionResult) -> EngineUsageObservation | None:
    payloads = _iter_json_objects(execution.stdout)
    transcript = f"{execution.stdout}\n{execution.stderr}".lower()
    if engine_name == "codex":
        for payload in payloads:
            text = json.dumps(payload)
            if "\"status\": 429" in text or "usage limit" in text:
                message = text
                return EngineUsageObservation(
                    source="provider",
                    provider="openai",
                    success=False,
                    limit_reason="usage limit reached",
                    limit_kind="quota",
                    metadata={
                        "error_status": 429,
                        "error_type": "rate_limit_error",
                        "retry_at_hint": "5:26 PM",
                        "purchase_more_credits": True,
                        "error_message": message,
                    },
                )
    if engine_name == "claude":
        for payload in payloads:
            error = payload.get("error")
            if isinstance(error, dict) and error.get("type") == "rate_limit_error":
                return EngineUsageObservation(
                    source="provider",
                    provider="anthropic",
                    success=False,
                    limit_reason="rate limit reached",
                    limit_kind="rate",
                    metadata={
                        "error_type": "rate_limit_error",
                        "error_message": error.get("message"),
                    },
                )
    if engine_name in {"goz", "opencode"}:
        for payload in payloads:
            if payload.get("type") == "step_finish":
                part = payload.get("part") or {}
                tokens = part.get("tokens") or {}
                total = tokens.get("total")
                if total is not None:
                    return EngineUsageObservation(
                        source="provider",
                        provider="z.ai",
                        success=execution.exit_code == 0,
                        usage=EngineUsageWindow(
                            used=total,
                            unit="tokens",
                        ),
                        metadata={
                            "finish_reason": part.get("reason"),
                            "input_tokens": tokens.get("input"),
                            "output_tokens": tokens.get("output"),
                            "reasoning_tokens": tokens.get("reasoning"),
                        },
                    )
    if "usage limit" in transcript:
        return EngineUsageObservation(
            source="local",
            success=False,
            limit_reason="usage limit reached",
            limit_kind="quota",
        )
    if "rate limit" in transcript:
        return EngineUsageObservation(
            source="local",
            success=False,
            limit_reason="rate limit reached",
            limit_kind="rate",
        )
    return None


def _build_timeline(
    engine_name: str,
    stdout: str,
    *,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> LiveTimeline | None:
    events: list[LiveEvent] = []
    for payload in _iter_json_objects(stdout):
        if "kind" in payload:
            try:
                event = UnifiedEvent.model_validate(payload)
            except Exception:
                continue
            events.append(
                LiveEvent(
                    kind=event.kind,
                    role=event.role,
                    content=event.content,
                    error=event.error,
                    tool_name=event.tool_name,
                    tool_input=event.tool_input,
                    tool_output=event.tool_output,
                    continuation_id=event.continuation_id,
                    metadata=event.metadata,
                )
            )
            continue
        payload_type = payload.get("type")
        if payload_type == "text":
            part = payload.get("part") or {}
            text = part.get("text")
            if isinstance(text, str):
                events.append(LiveEvent(kind="message", content=text))
        elif payload_type == "step_finish":
            events.append(LiveEvent(kind="usage", metadata=payload))
    if not events:
        return None
    timeline = LiveTimeline(engine=engine_name, task_id=task_id, subagent_id=subagent_id, events=events)
    timeline.recompute_counts()
    return timeline


def extract_engine_timeline(
    engine_name: str,
    stdout: str,
    *,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> LiveTimeline | None:
    return _build_timeline(engine_name, stdout, task_id=task_id, subagent_id=subagent_id)


def extract_engine_continuation(
    engine_name: str,
    execution: CLIExecutionResult | None,
) -> RuntimeEngineContinuation | None:
    del engine_name
    if execution is None:
        return None
    for payload in _iter_json_objects(execution.stdout):
        continuation_id = payload.get("continuation_id") or payload.get("sessionID")
        if isinstance(continuation_id, str) and continuation_id:
            return RuntimeEngineContinuation(session_id=continuation_id)
    return None


def classify_execution_interruption(transcript: str, *, exit_code: int | None = None) -> str | None:
    if exit_code in {130, 143}:
        return "interrupted"
    normalized = transcript.lower()
    if "interrupted" in normalized or "cancelled" in normalized:
        return "interrupted"
    return None


def classify_execution_limit(transcript: str) -> str | None:
    normalized = transcript.lower()
    if "usage limit" in normalized:
        return "usage limit reached"
    if "rate limit" in normalized:
        return "rate limit reached"
    return None


def classify_retryable_execution_failure(transcript: str):
    normalized = transcript.lower()
    if "timeout" in normalized:
        return RetryableExecutionFailure("transient timeout", classification="timeout", kind="timeout")
    if "connection" in normalized:
        return RetryableExecutionFailure(
            "transient connection failure",
            classification="connection_error",
            kind="connection",
        )
    return None


def filter_supported_kwargs(func: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    signature = inspect.signature(func)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in signature.parameters}


def effective_engine_callable(adapter: Any, name: str) -> Any:
    instance_attr = adapter.__dict__.get(name)
    if callable(instance_attr):
        return instance_attr
    return getattr(adapter, name)


def has_callable_override(adapter: Any, name: str, original: Any) -> bool:
    instance_attr = adapter.__dict__.get(name)
    if callable(instance_attr):
        return True
    cls_attr = getattr(type(adapter), name, None)
    return cls_attr is not None and cls_attr is not original


def supports_live_execution(adapter: Any) -> bool:
    return has_callable_override(adapter, "run_live", ExternalCLIAdapter.run_live) and not has_callable_override(
        adapter,
        "run",
        ExternalCLIAdapter.run,
    )


def supports_live_on_started(adapter: Any) -> bool:
    return "on_started" in inspect.signature(effective_engine_callable(adapter, "run_live")).parameters


def supports_on_started(adapter: Any) -> bool:
    return "on_started" in inspect.signature(effective_engine_callable(adapter, "run")).parameters


ORIGINAL_EXTERNAL_ADAPTER_RUN = ExternalCLIAdapter.run
ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE = ExternalCLIAdapter.run_live


def _make_engine(name: str) -> ExternalCLIAdapter:
    capabilities = AdapterCapabilities(
        supports_model_override=name in {"claude", "gemini", "goz", "opencode"},
        transcript_format="jsonl" if name in {"codex", "claude", "goz", "opencode"} else "text",
        strips_environment=False,
    )
    binary = "zai" if name == "goz" else name
    return ExternalCLIAdapter(name=name, binary=binary, capabilities=capabilities)


_ENGINE_REGISTRY = {name: _make_engine(name) for name in ENGINE_CHOICES}


def get_engine(name: str) -> ExternalCLIAdapter:
    return _ENGINE_REGISTRY[name]


def _make_quota_module(check_name: str) -> types.ModuleType:
    module = types.ModuleType(check_name)

    def _check() -> UsageStatus:
        return UsageStatus()

    def _reset_cache() -> None:
        return None

    setattr(module, check_name, _check)
    setattr(module, "UsageStatus", UsageStatus)
    setattr(module, "UsageWindow", UsageWindow)
    setattr(module, "reset_cache", _reset_cache)
    setattr(module, "codex_quota_block_reason", lambda **_: None)
    return module


def install_heru_stub() -> None:
    if "heru" in sys.modules:
        return

    heru_mod = types.ModuleType("heru")
    heru_mod.ENGINE_CHOICES = ENGINE_CHOICES
    heru_mod.get_engine = get_engine
    heru_mod.iter_jsonl_payloads = iter_jsonl_payloads
    heru_mod.extract_engine_timeline = extract_engine_timeline
    heru_mod.extract_engine_continuation = extract_engine_continuation
    heru_mod.RetryableExecutionFailure = RetryableExecutionFailure

    base_mod = types.ModuleType("heru.base")
    base_mod.AdapterCapabilities = AdapterCapabilities
    base_mod.CLIExecutionResult = CLIExecutionResult
    base_mod.CLIInvocation = CLIInvocation
    base_mod.ExternalCLIAdapter = ExternalCLIAdapter

    types_mod = types.ModuleType("heru.types")
    types_mod.EngineUsageObservation = EngineUsageObservation
    types_mod.EngineUsageWindow = EngineUsageWindow
    types_mod.EngineLimitKind = EngineLimitKind
    types_mod.EngineMonitoringSource = EngineMonitoringSource
    types_mod.LiveEvent = LiveEvent
    types_mod.LiveEventKind = LiveEventKind
    types_mod.LiveEventRole = LiveEventRole
    types_mod.LiveTimeline = LiveTimeline
    types_mod.ResourceLimitEvent = ResourceLimitEvent
    types_mod.RuntimeEngineContinuation = RuntimeEngineContinuation
    types_mod.SubagentRef = SubagentRef
    types_mod.SubagentStatus = SubagentStatus
    types_mod.UnifiedEvent = UnifiedEvent

    adapters_mod = types.ModuleType("heru.adapters")
    adapters_mod.EngineError = EngineError
    adapters_mod.classify_execution_interruption = classify_execution_interruption
    adapters_mod.classify_execution_limit = classify_execution_limit
    adapters_mod.classify_retryable_execution_failure = classify_retryable_execution_failure

    detection_mod = types.ModuleType("heru.engine_detection")
    detection_mod.ORIGINAL_EXTERNAL_ADAPTER_RUN = ORIGINAL_EXTERNAL_ADAPTER_RUN
    detection_mod.ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE = ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE
    detection_mod.effective_engine_callable = effective_engine_callable
    detection_mod.filter_supported_kwargs = filter_supported_kwargs
    detection_mod.has_callable_override = has_callable_override
    detection_mod.supports_live_execution = supports_live_execution
    detection_mod.supports_live_on_started = supports_live_on_started
    detection_mod.supports_on_started = supports_on_started

    quota_pkg = types.ModuleType("heru.quota")
    quota_pkg.UsageStatus = UsageStatus
    quota_pkg.UsageWindow = UsageWindow

    claude_quota_mod = _make_quota_module("check_claude_quota")
    codex_quota_mod = _make_quota_module("check_codex_quota")
    copilot_quota_mod = _make_quota_module("check_copilot_quota")
    zai_quota_mod = _make_quota_module("check_zai_quota")

    sys.modules["heru"] = heru_mod
    sys.modules["heru.base"] = base_mod
    sys.modules["heru.types"] = types_mod
    sys.modules["heru.adapters"] = adapters_mod
    sys.modules["heru.engine_detection"] = detection_mod
    sys.modules["heru.quota"] = quota_pkg
    sys.modules["heru.quota.claude_quota"] = claude_quota_mod
    sys.modules["heru.quota.codex_quota"] = codex_quota_mod
    sys.modules["heru.quota.copilot_quota"] = copilot_quota_mod
    sys.modules["heru.quota.zai_quota"] = zai_quota_mod

    heru_mod.base = base_mod
    heru_mod.types = types_mod
    heru_mod.adapters = adapters_mod
    heru_mod.engine_detection = detection_mod
    heru_mod.quota = quota_pkg
    quota_pkg.claude_quota = claude_quota_mod
    quota_pkg.codex_quota = codex_quota_mod
    quota_pkg.copilot_quota = copilot_quota_mod
    quota_pkg.zai_quota = zai_quota_mod
