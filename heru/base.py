"""Public base adapter primitives for heru.

This module defines the stable base contract that external callers and
adapter subclasses may program against: adapter capabilities, immutable
invocation and execution records, and the ``ExternalCLIAdapter`` class.
"""

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import json
import logging
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time
from typing import Callable, Literal

from heru.types import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
    LiveTimeline,
    RuntimeEngineContinuation,
    UnifiedEvent,
    utcnow,
)

logger = logging.getLogger("litehive.agents.base")


TranscriptFormat = Literal["text", "jsonl"]
_CALLER_WORKSPACE_ENV_VAR = "LITEHIVE_WORKSPACE_ROOT"
LATEST_CONTINUATION_SENTINEL = "__heru_continue_latest__"
_INHERITED_PYTHON_ENV_VARS = (
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "__PYVENV_LAUNCHER__",
)


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.resolve().relative_to(workspace_root.resolve())
        return True
    except ValueError:
        return False


def build_invocation_env(
    *,
    cwd: Path,
    stripped_env_vars: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    caller_workspace = (extra_env or {}).get(_CALLER_WORKSPACE_ENV_VAR, env.get(_CALLER_WORKSPACE_ENV_VAR))
    if caller_workspace:
        try:
            workspace_root = Path(caller_workspace)
        except OSError:
            workspace_root = None
        if workspace_root is not None and not _is_within_workspace(cwd, workspace_root):
            for key in _INHERITED_PYTHON_ENV_VARS:
                env.pop(key, None)
    for key in stripped_env_vars:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    return env


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """Capability flags advertised by an adapter instance."""

    available: bool = False
    supports_model_override: bool = False
    strips_environment: bool = False
    transcript_format: TranscriptFormat = "text"


@dataclass(frozen=True, slots=True)
class CLIInvocation:
    """Public immutable launch description passed into an external CLI run."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdin_data: str | None = None


@dataclass(frozen=True, slots=True)
class CLIExecutionResult:
    """Public immutable record for a finished external CLI execution."""

    adapter: str
    argv: tuple[str, ...]
    cwd: Path
    exit_code: int
    stdout: str
    stderr: str
    pid: int | None = None
    sandboxed: bool = False
    sandbox_summary: str = ""

    @property
    def returncode(self) -> int:
        return self.exit_code

    @property
    def transcript(self) -> str:
        parts = [self.stdout.strip()]
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.strip()}")
        return "\n\n".join(part for part in parts if part).strip()


@dataclass(frozen=True, slots=True)
class StreamEventAdapter:
    """Adapter hooks for JSONL event streams with partial live output."""

    unwrap_event: Callable[[dict[str, object]], dict[str, object]] | None = None
    text_deltas: Callable[[dict[str, object]], list[tuple[int, str]]] | None = None
    final_messages: Callable[[dict[str, object]], list[str]] | None = None
    errors: Callable[[dict[str, object]], list[str]] | None = None
    live_events: Callable[[dict[str, object]], list[LiveEvent]] | None = None
    continuation_id: Callable[[dict[str, object]], str | None] | None = None

    def unwrap(self, payload: dict[str, object]) -> dict[str, object]:
        if self.unwrap_event is None:
            return payload
        return self.unwrap_event(payload)

    def extract_text_deltas(self, payload: dict[str, object]) -> list[tuple[int, str]]:
        if self.text_deltas is None:
            return []
        return self.text_deltas(payload)

    def extract_final_messages(self, payload: dict[str, object]) -> list[str]:
        if self.final_messages is None:
            return []
        return self.final_messages(payload)

    def extract_errors(self, payload: dict[str, object]) -> list[str]:
        if self.errors is None:
            return []
        return self.errors(payload)

    def extract_live_events(self, payload: dict[str, object]) -> list[LiveEvent]:
        if self.live_events is None:
            return []
        return self.live_events(payload)

    def extract_continuation_id(self, payload: dict[str, object]) -> str | None:
        if self.continuation_id is None:
            return None
        return self.continuation_id(payload)


ObservationMetadata = dict[str, str | int | bool | None]


@dataclass(slots=True)
class UsageScanState:
    usage: EngineUsageWindow | None = None
    limit_reason: str | None = None
    metadata: ObservationMetadata = field(default_factory=dict)
    done: bool = False


class ExternalCLIAdapter:
    """Public base class for stable heru engine adapters.

    Subclasses may override command-building, transcript parsing, usage
    extraction, and continuation hooks while callers rely on the shared
    run/build contract remaining stable across releases.
    """

    LIVE_UPDATE_INTERVAL_SECONDS = 0.5
    DEFAULT_NAME: str | None = None
    DEFAULT_BINARY: str | None = None
    DEFAULT_CAPABILITIES = AdapterCapabilities()
    DEFAULT_STRIPPED_ENV_VARS: tuple[str, ...] = ()
    SUPPORTS_CONTINUE_LATEST = False
    TRANSCRIPT_EMPTY_ON_PARSED_PAYLOADS = False
    USAGE_PROVIDER: str | None = None
    REQUIRE_USAGE_PAYLOADS = False

    def __init__(
        self,
        *,
        name: str | None = None,
        binary: str | None = None,
        capabilities: AdapterCapabilities | None = None,
        stripped_env_vars: tuple[str, ...] | None = None,
    ) -> None:
        resolved_name = name if name is not None else self.DEFAULT_NAME
        resolved_binary = binary if binary is not None else self.DEFAULT_BINARY
        resolved_capabilities = (
            capabilities if capabilities is not None else self.DEFAULT_CAPABILITIES
        )
        resolved_stripped_env_vars = (
            stripped_env_vars
            if stripped_env_vars is not None
            else self.DEFAULT_STRIPPED_ENV_VARS
        )
        if resolved_name is None or resolved_binary is None:
            raise ValueError("Adapter subclasses must define default name and binary")
        self.name = resolved_name
        self.binary = resolved_binary
        self.capabilities = resolved_capabilities
        self.stripped_env_vars = resolved_stripped_env_vars

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def detect_capabilities(self) -> AdapterCapabilities:
        return replace(self.capabilities, available=self.is_available())

    def supports_continue_latest(self) -> bool:
        return self.SUPPORTS_CONTINUE_LATEST

    def is_latest_continuation(self, resume_session_id: str | None) -> bool:
        return resume_session_id == LATEST_CONTINUATION_SENTINEL

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def build_invocation(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> CLIInvocation:
        return CLIInvocation(
            argv=tuple(self.build_command(
                prompt, cwd, model=model, max_turns=max_turns,
                resume_session_id=resume_session_id,
            )),
            cwd=cwd,
            env=build_invocation_env(
                cwd=cwd,
                stripped_env_vars=self.stripped_env_vars,
                extra_env=extra_env,
            ),
        )

    def finalize_invocation(self, invocation: CLIInvocation) -> CLIInvocation:
        return invocation

    def sandbox_details(self) -> tuple[bool, str]:
        return (False, "")

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started: Callable[[int], None] | None = None,
        extra_env: dict[str, str] | None = None,
        emit_unified: bool = False,
    ) -> CLIExecutionResult:
        invocation = self.finalize_invocation(
            self.build_invocation(prompt, cwd, model=model, max_turns=max_turns,
                                  resume_session_id=resume_session_id, extra_env=extra_env)
        )
        sandboxed, sandbox_summary = self.sandbox_details()
        proc = subprocess.Popen(
            invocation.argv,
            cwd=str(invocation.cwd),
            env=invocation.env,
            stdin=subprocess.PIPE if invocation.stdin_data else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if on_started is not None:
            on_started(proc.pid)
        stdout, stderr = proc.communicate(input=invocation.stdin_data)
        if emit_unified:
            stdout = self.render_unified_output(stdout)
        return CLIExecutionResult(
            adapter=self.name,
            argv=invocation.argv,
            cwd=invocation.cwd,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            pid=proc.pid,
            sandboxed=sandboxed,
            sandbox_summary=sandbox_summary,
        )

    def run_live(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started: Callable[[int], None] | None = None,
        on_update: Callable[[CLIExecutionResult], None] | None = None,
        inactivity_timeout_seconds: float = 0,
        extra_env: dict[str, str] | None = None,
        emit_unified: bool = False,
    ) -> CLIExecutionResult:
        invocation = self.finalize_invocation(
            self.build_invocation(prompt, cwd, model=model, max_turns=max_turns,
                                  resume_session_id=resume_session_id, extra_env=extra_env)
        )
        sandboxed, sandbox_summary = self.sandbox_details()
        proc = subprocess.Popen(
            invocation.argv,
            cwd=str(invocation.cwd),
            stdin=subprocess.PIPE if invocation.stdin_data else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=invocation.env,
            text=False,
        )
        if invocation.stdin_data and proc.stdin is not None:
            proc.stdin.write(invocation.stdin_data.encode("utf-8"))
            proc.stdin.close()
            proc.stdin = None
        if on_started is not None:
            on_started(proc.pid)
        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_chunks = bytearray()
        stderr_chunks = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, data="stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, data="stderr")
        last_update_at = time.monotonic()

        def drain_after_abort() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if proc.stdout is not None:
                stdout_chunks.extend(proc.stdout.read() or b"")
                proc.stdout.close()
            if proc.stderr is not None:
                stderr_chunks.extend(proc.stderr.read() or b"")
                proc.stderr.close()

        def emit_update() -> None:
            if on_update is None:
                return
            stdout = stdout_chunks.decode("utf-8", errors="replace")
            if emit_unified:
                stdout = self._render_live_unified_output(stdout)
            on_update(
                CLIExecutionResult(
                    adapter=self.name,
                    argv=invocation.argv,
                    cwd=invocation.cwd,
                    exit_code=proc.poll() or 0,
                    stdout=stdout,
                    stderr=stderr_chunks.decode("utf-8", errors="replace"),
                    pid=proc.pid,
                    sandboxed=sandboxed,
                    sandbox_summary=sandbox_summary,
                )
            )

        last_output_at = time.monotonic()

        try:
            while selector.get_map():
                events = selector.select(timeout=self.LIVE_UPDATE_INTERVAL_SECONDS)

                # Always check inactivity timeout, even when data is flowing
                if (
                    inactivity_timeout_seconds > 0
                    and proc.poll() is None
                    and time.monotonic() - last_output_at > inactivity_timeout_seconds
                ):
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    stderr_chunks.extend(
                        f"\n[litehive] Process killed after {inactivity_timeout_seconds:.0f}s of inactivity.\n".encode()
                    )
                    break

                if not events:
                    if (
                        proc.poll() is None
                        and time.monotonic() - last_update_at >= self.LIVE_UPDATE_INTERVAL_SECONDS
                    ):
                        emit_update()
                        last_update_at = time.monotonic()
                    continue
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if chunk:
                        if key.data == "stdout":
                            stdout_chunks.extend(chunk)
                            last_output_at = time.monotonic()
                        else:
                            stderr_chunks.extend(chunk)
                        emit_update()
                        last_update_at = time.monotonic()
                        continue
                    selector.unregister(key.fileobj)
                    key.fileobj.close()

            exit_code = proc.wait()
            stdout = stdout_chunks.decode("utf-8", errors="replace")
            if emit_unified:
                stdout = self.render_unified_output(stdout)
            result = CLIExecutionResult(
                adapter=self.name,
                argv=invocation.argv,
                cwd=invocation.cwd,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr_chunks.decode("utf-8", errors="replace"),
                pid=proc.pid,
                sandboxed=sandboxed,
                sandbox_summary=sandbox_summary,
            )
            if on_update is not None:
                on_update(result)
            return result
        except BaseException:
            drain_after_abort()
            raise
        finally:
            selector.close()

    def extract_stream_transcript_text(
        self,
        stdout: str,
        *,
        delta_fallback: Callable[[str], list[str]] | None = None,
    ) -> str:
        adapter = self.stream_event_adapter()
        if adapter is None:
            return ""
        return extract_stream_transcript(stdout, adapter=adapter, delta_fallback=delta_fallback)

    def extract_stream_error_messages(self, stdout: str) -> list[str]:
        adapter = self.stream_event_adapter()
        if adapter is None:
            return []
        return extract_stream_errors(stdout, adapter=adapter)

    def transcript_assistant_text(self, execution: CLIExecutionResult) -> str:
        return ""

    def transcript_error_text(self, execution: CLIExecutionResult) -> str:
        return ""

    def transcript_empty_on_parsed_payloads(self) -> bool:
        return self.TRANSCRIPT_EMPTY_ON_PARSED_PAYLOADS

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = self.transcript_assistant_text(execution)
        error_text = self.transcript_error_text(execution)
        if assistant_text or error_text or self.transcript_empty_on_parsed_payloads():
            return self.render_transcript_from_parts(
                execution,
                assistant_text=assistant_text,
                error_text=error_text,
                empty_on_parsed_payloads=self.transcript_empty_on_parsed_payloads(),
            )
        return execution.transcript

    def render_transcript_from_parts(
        self,
        execution: CLIExecutionResult,
        *,
        assistant_text: str,
        error_text: str = "",
        empty_on_parsed_payloads: bool = False,
    ) -> str:
        if assistant_text or error_text:
            parts = [part for part in (assistant_text, error_text) if part]
            if execution.stderr.strip():
                parts.append(f"[stderr]\n{execution.stderr.strip()}")
            return "\n\n".join(parts)
        if empty_on_parsed_payloads and self.iter_native_payloads(execution.stdout):
            return f"[stderr]\n{execution.stderr.strip()}" if execution.stderr.strip() else ""
        return execution.transcript

    def usage_observation_from_scan(
        self,
        execution: CLIExecutionResult,
        *,
        provider: str,
        usage: EngineUsageWindow | None,
        limit_reason: str | None,
        metadata: ObservationMetadata,
        saw_payloads: bool,
        require_payloads: bool = False,
        stderr_limit_extractor: Callable[
            [str, ObservationMetadata],
            str | None,
        ]
        | None = None,
    ) -> EngineUsageObservation | None:
        if limit_reason is None and execution.stderr.strip() and stderr_limit_extractor is not None:
            limit_reason = stderr_limit_extractor(execution.stderr, metadata)
        if require_payloads and not saw_payloads:
            return None
        if usage is None and limit_reason is None and not metadata:
            return None
        return EngineUsageObservation(
            source="provider" if saw_payloads else "local",
            provider=provider,
            success=execution.exit_code == 0,
            limit_reason=limit_reason,
            usage=usage,
            metadata=metadata,
        )

    def usage_provider(self) -> str | None:
        return self.USAGE_PROVIDER

    def require_usage_payloads(self) -> bool:
        return self.REQUIRE_USAGE_PAYLOADS

    def iter_usage_payloads(self, stdout: str) -> list[dict[str, object]]:
        return self.iter_native_payloads(stdout)

    def usage_window_from_payload(
        self,
        payload: dict[str, object],
        metadata: ObservationMetadata,
    ) -> EngineUsageWindow | None:
        return None

    def error_details_from_payload(
        self,
        payload: dict[str, object],
    ) -> tuple[str | None, ObservationMetadata, EngineUsageWindow | None]:
        return None, {}, None

    def update_usage_metadata_from_payload(
        self,
        payload: dict[str, object],
        metadata: ObservationMetadata,
    ) -> None:
        return None

    def classify_limit_text(
        self,
        text: str,
        metadata: ObservationMetadata,
    ) -> str | None:
        from heru.adapters.common import classify_execution_limit

        return classify_execution_limit(text)

    def classify_stderr_limit(
        self,
        stderr: str,
        metadata: ObservationMetadata,
    ) -> str | None:
        return None

    def scan_usage_payload(
        self,
        payload: dict[str, object],
        state: UsageScanState,
    ) -> None:
        if state.usage is None:
            state.usage = self.usage_window_from_payload(payload, state.metadata)
        error_message, error_metadata, error_usage = self.error_details_from_payload(payload)
        if error_metadata:
            state.metadata.update(error_metadata)
        if state.usage is None and error_usage is not None:
            state.usage = error_usage
        if state.limit_reason is None and error_message:
            state.metadata.setdefault("error_message", error_message)
            state.limit_reason = self.classify_limit_text(error_message, state.metadata)
        self.update_usage_metadata_from_payload(payload, state.metadata)

    def extract_usage_observation(
        self,
        execution: CLIExecutionResult,
    ) -> EngineUsageObservation | None:
        provider = self.usage_provider()
        if provider is None:
            return None
        payloads = self.iter_usage_payloads(execution.stdout)
        state = UsageScanState()
        for payload in reversed(payloads):
            self.scan_usage_payload(payload, state)
            if state.done:
                break
        stderr_limit_extractor = (
            None
            if type(self).classify_stderr_limit is ExternalCLIAdapter.classify_stderr_limit
            else self.classify_stderr_limit
        )
        return self.usage_observation_from_scan(
            execution,
            provider=provider,
            usage=state.usage,
            limit_reason=state.limit_reason,
            metadata=state.metadata,
            saw_payloads=bool(payloads),
            require_payloads=self.require_usage_payloads(),
            stderr_limit_extractor=stderr_limit_extractor,
        )

    def stream_event_adapter(self) -> StreamEventAdapter | None:
        return None

    def iter_native_payloads(self, stdout: str) -> list[dict[str, object]]:
        return iter_jsonl_payloads(stdout)

    def _iter_live_native_payloads(self, stdout: str) -> list[dict[str, object]]:
        return self.iter_native_payloads(stdout)

    def translate_native_event(
        self,
        native_payload: dict[str, object],
    ) -> UnifiedEvent | None:
        events = self.translate_native_events(native_payload)
        return events[0] if events else None

    def translate_native_events(
        self,
        native_payload: dict[str, object],
    ) -> list[UnifiedEvent]:
        adapter = self.stream_event_adapter()
        payload = adapter.unwrap(native_payload) if adapter is not None else native_payload
        raw = native_payload if isinstance(native_payload, dict) else {}
        unified_events: list[UnifiedEvent] = []
        if adapter is not None:
            for event in adapter.extract_live_events(payload):
                unified_events.append(self._live_event_to_unified_event(event, raw=raw))
            continuation_id = adapter.extract_continuation_id(payload)
            if continuation_id:
                unified_events.append(
                    UnifiedEvent(
                        kind="continuation",
                        engine=self.name,
                        continuation_id=continuation_id,
                        raw=raw,
                    )
                )
        return unified_events

    def render_unified_output(self, stdout: str) -> str:
        return self._render_unified_output_from_payloads(self.iter_native_payloads(stdout))

    def _render_live_unified_output(self, stdout: str) -> str:
        return self._render_unified_output_from_payloads(self._iter_live_native_payloads(stdout))

    def _render_unified_output_from_payloads(
        self,
        payloads: list[dict[str, object]],
    ) -> str:
        unified_lines: list[str] = []
        sequence = 0
        final_continuation_id: str | None = None
        for payload in payloads:
            for event in self.translate_native_events(payload):
                if event.kind == "continuation":
                    final_continuation_id = event.continuation_id or event.content or final_continuation_id
                    continue
                event.sequence = sequence
                if not event.engine:
                    event.engine = self.name
                unified_lines.append(event.model_dump_json(exclude_none=True))
                sequence += 1
        if final_continuation_id:
            unified_lines.append(
                UnifiedEvent(
                    kind="continuation",
                    engine=self.name,
                    sequence=sequence,
                    continuation_id=final_continuation_id,
                ).model_dump_json(exclude_none=True)
            )
        return ("\n".join(unified_lines) + "\n") if unified_lines else ""

    def _live_event_to_unified_event(
        self,
        event: LiveEvent,
        *,
        raw: dict[str, object],
    ) -> UnifiedEvent:
        usage_delta = event.usage_delta or (event.metadata if event.kind == "usage" else {})
        continuation_id = event.continuation_id
        if not continuation_id and event.kind == "continuation":
            continuation_id = event.content or None
        return UnifiedEvent(
            kind=event.kind,
            engine=event.engine or self.name,
            sequence=event.sequence,
            timestamp=event.timestamp or utcnow(),
            role=event.role,
            content=event.content,
            tool_name=event.tool_name,
            tool_input=event.tool_input,
            tool_output=event.tool_output,
            error=event.error,
            usage_delta=usage_delta,
            continuation_id=continuation_id,
            raw=raw,
            metadata=event.metadata,
        )

    def continuation_from_payload(
        self,
        payload: dict[str, object],
    ) -> RuntimeEngineContinuation | None:
        return None

    def iter_continuation_payloads(self, stdout: str) -> list[dict[str, object]]:
        return self.iter_native_payloads(stdout)

    def extract_continuation(
        self,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        return self.extract_continuation_from_payloads(execution, self.continuation_from_payload)

    def extract_continuation_from_payloads(
        self,
        execution: CLIExecutionResult | None,
        extractor: Callable[[dict[str, object]], RuntimeEngineContinuation | None],
        payloads: list[dict[str, object]] | None = None,
    ) -> RuntimeEngineContinuation | None:
        if execution is None or not execution.stdout.strip():
            return None
        payload_list = payloads if payloads is not None else self.iter_continuation_payloads(execution.stdout)
        for payload in payload_list:
            continuation = extractor(payload)
            if continuation is not None:
                return continuation
        return None


def extract_jsonl_messages(stdout: str) -> str:
    parts: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        content: str | None = None
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            raw_content = payload.get("content")
            if isinstance(raw_content, str) and raw_content:
                content = raw_content
        elif payload.get("type") == "assistant.message":
            data = payload.get("data")
            if isinstance(data, dict):
                raw_content = data.get("content")
                if isinstance(raw_content, str) and raw_content:
                    content = raw_content
        if content is None:
            continue
        parts.append(content)
    return "".join(parts).strip()


def extract_stream_transcript(
    stdout: str,
    *,
    adapter: StreamEventAdapter,
    delta_fallback: Callable[[str], list[str]] | None = None,
) -> str:
    messages: list[str] = []
    partial_blocks: dict[int, list[str]] = {}
    for raw_payload in iter_jsonl_payloads(stdout):
        payload = adapter.unwrap(raw_payload)
        for message in adapter.extract_final_messages(payload):
            if message:
                messages.append(message)
        for index, text in adapter.extract_text_deltas(payload):
            if not text:
                continue
            partial_blocks.setdefault(index, []).append(text)
    if not messages and partial_blocks:
        messages.extend("".join(partial_blocks[index]) for index in sorted(partial_blocks))
    if not messages and delta_fallback is not None:
        messages.extend(text for text in delta_fallback(stdout) if text)
    return "\n".join(messages).strip()


def extract_stream_errors(stdout: str, *, adapter: StreamEventAdapter) -> list[str]:
    errors: list[str] = []
    for raw_payload in iter_jsonl_payloads(stdout):
        payload = adapter.unwrap(raw_payload)
        errors.extend(error for error in adapter.extract_errors(payload) if error)
    return errors


def iter_jsonl_payloads(stdout: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(stdout.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "iter_jsonl_payloads: skipping unparseable line %d: %s (content: %.200s)",
                line_number,
                exc,
                line,
            )
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
        else:
            logger.warning(
                "iter_jsonl_payloads: skipping non-object JSON at line %d (type=%s, content: %.200s)",
                line_number,
                type(payload).__name__,
                line,
            )
    return payloads


def extract_jsonl_errors(stdout: str) -> list[str]:
    errors: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") == "tool_result" and payload.get("status") == "error":
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                errors.append(error["message"])
            continue

        if payload.get("type") == "error":
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                errors.append(data["message"])
    return errors


def extract_codex_messages(stdout: str) -> str:
    messages: OrderedDict[str, str] = OrderedDict()
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") not in {"item.completed", "item.updated"}:
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            messages[item_id] = text
    return "\n".join(messages.values()).strip()


def extract_codex_errors(stdout: str) -> list[str]:
    errors: list[str] = []
    command_errors: OrderedDict[str, str] = OrderedDict()
    for payload in iter_jsonl_payloads(stdout):
        event_type = payload.get("type")
        if event_type in {"error", "turn.failed"}:
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                errors.append(message.strip())
            continue

        if event_type not in {"item.completed", "item.updated"}:
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "command_execution":
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        aggregated_output = item.get("aggregated_output")
        if isinstance(aggregated_output, str) and aggregated_output.strip():
            if item.get("status") == "failed" or item.get("exit_code") not in {None, 0}:
                command_errors[item_id] = aggregated_output.strip()
                continue
        command_errors.pop(item_id, None)
    return [*errors, *command_errors.values()]


def extract_live_timeline(
    stdout: str,
    *,
    engine: str,
    adapter: StreamEventAdapter | None = None,
) -> LiveTimeline:
    events: list[LiveEvent] = []
    sequence = 0
    for raw_payload in iter_jsonl_payloads(stdout):
        payload = adapter.unwrap(raw_payload) if adapter is not None else raw_payload
        if adapter is not None:
            for event in adapter.extract_live_events(payload):
                events.append(
                    LiveEvent(
                        kind=event.kind,
                        engine=event.engine or engine,
                        sequence=sequence,
                        timestamp=event.timestamp or utcnow(),
                        role=event.role,
                        content=event.content,
                        tool_name=event.tool_name,
                        tool_input=event.tool_input,
                        tool_output=event.tool_output,
                        error=event.error,
                        usage_delta=event.usage_delta,
                        continuation_id=event.continuation_id,
                        raw=event.raw,
                        metadata=event.metadata,
                    )
                )
                sequence += 1
            continue
        for message in _generic_final_messages(payload):
            events.append(
                LiveEvent(
                    kind="message",
                    engine=engine,
                    sequence=sequence,
                    role="assistant",
                    content=message,
                )
            )
            sequence += 1
        for error in _generic_errors(payload):
            events.append(
                LiveEvent(
                    kind="error",
                    engine=engine,
                    sequence=sequence,
                    error=error,
                )
            )
            sequence += 1
    timeline = LiveTimeline(engine=engine)
    timeline.events = events
    timeline.recompute_counts()
    return timeline


def _generic_final_messages(payload: dict[str, object]) -> list[str]:
    content: str | None = None
    if payload.get("type") == "message" and payload.get("role") == "assistant":
        raw_content = payload.get("content")
        if isinstance(raw_content, str) and raw_content:
            content = raw_content
    elif payload.get("type") == "assistant.message":
        data = payload.get("data")
        if isinstance(data, dict):
            raw_content = data.get("content")
            if isinstance(raw_content, str) and raw_content:
                content = raw_content
    elif payload.get("type") == "item.completed":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                content = text
    if content is None:
        return []
    return [content]


def _generic_errors(payload: dict[str, object]) -> list[str]:
    if payload.get("type") == "error":
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return [data["message"]]
        message = payload.get("message")
        if isinstance(message, str) and message:
            return [message]
    return []
