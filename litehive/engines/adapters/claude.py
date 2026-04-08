"""Claude CLI engine adapter."""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

from litehive.engines.adapters.common import _decode_json_object, classify_execution_limit
from litehive.engines.base import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
    StreamEventAdapter,
    extract_jsonl_errors,
    extract_stream_errors,
    extract_stream_transcript,
    iter_jsonl_payloads,
    parse_stage_report_text,
)
from litehive.models import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
    RuntimeEngineContinuation,
)


class ClaudeCLIAdapter(ExternalCLIAdapter):
    DEFAULT_MODEL = "claude-sonnet-4-20250514"
    DEFAULT_NAME = "claude"
    DEFAULT_BINARY = "claude"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=True,
        strips_environment=False,
        transcript_format="jsonl",
    )

    def __init__(
        self,
        *,
        name: str | None = None,
        binary: str | None = None,
        capabilities: AdapterCapabilities | None = None,
        stripped_env_vars: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(
            name=name, binary=binary, capabilities=capabilities, stripped_env_vars=stripped_env_vars
        )

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        command = [self.binary]
        if resume_session_id:
            command.extend(["--resume", resume_session_id, "-p", prompt])
        else:
            command.extend(["-p", prompt])
        command.extend(
            [
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--dangerously-skip-permissions",
            ]
        )
        if model:
            command.extend(["--model", model])
        if max_turns is not None:
            command.extend(["--max-turns", str(max_turns)])
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = extract_stream_transcript(
            execution.stdout,
            adapter=self.stream_event_adapter(),
            delta_fallback=_extract_claude_text_delta_fallback,
        )
        if assistant_text:
            if execution.stderr.strip():
                return f"{assistant_text}\n\n[stderr]\n{execution.stderr.strip()}"
            return assistant_text
        return execution.transcript

    def parse_stage_report(
        self,
        *,
        task_id: str,
        step: str,
        execution: CLIExecutionResult,
        subagent_status: str,
    ):
        transcript = self.render_transcript(execution)
        if transcript == execution.transcript:
            stderr_lines = extract_stream_errors(
                execution.stdout,
                adapter=self.stream_event_adapter(),
            ) or extract_jsonl_errors(execution.stdout)
            if stderr_lines:
                transcript = "\n".join(stderr_lines)
        return parse_stage_report_text(
            task_id=task_id,
            step=step,  # type: ignore[arg-type]
            transcript=transcript,
            subagent_status=subagent_status,  # type: ignore[arg-type]
        )

    def extract_usage_observation(
        self,
        execution: CLIExecutionResult,
    ) -> EngineUsageObservation | None:
        payloads = iter_jsonl_payloads(execution.stdout)
        for payload in reversed(payloads):
            error_message, error_metadata = _claude_error_details(payload)
            if payload.get("type") != "result":
                if error_message is None and not error_metadata:
                    continue
                return EngineUsageObservation(
                    source="provider",
                    provider="anthropic",
                    success=False,
                    limit_reason=classify_execution_limit(error_message) if error_message else None,
                    metadata=error_metadata,
                )
            metadata: dict[str, str | int | bool | None] = dict(error_metadata)
            usage = _claude_usage_window(payload, metadata)
            if usage is None and error_message is None and not metadata:
                continue
            return EngineUsageObservation(
                source="provider",
                provider="anthropic",
                success=not bool(payload.get("is_error")),
                limit_reason=classify_execution_limit(error_message) if error_message else None,
                usage=usage,
                metadata=metadata,
            )
        return None
    def stream_event_adapter(self) -> StreamEventAdapter:
        return StreamEventAdapter(
            unwrap_event=self._unwrap_stream_event,
            text_deltas=self._text_deltas,
            final_messages=self._final_messages,
            errors=self._errors,
            live_events=self._live_events,
        )

    def extract_continuation(
        self,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        if execution is None or not execution.stdout.strip():
            return None
        for payload in iter_jsonl_payloads(execution.stdout):
            if payload.get("type") == "system" and payload.get("subtype") == "init":
                session_id = payload.get("session_id")
                if isinstance(session_id, str) and session_id:
                    return RuntimeEngineContinuation(session_id=session_id)
        return None

    @staticmethod
    def _final_messages(payload: dict[str, object]) -> list[str]:
        messages: list[str] = []
        event_type = payload.get("type")
        if event_type == "assistant":
            data = payload.get("message")
            if isinstance(data, dict):
                content = data.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if isinstance(text, str) and text:
                                messages.append(text)
                elif isinstance(content, str) and content:
                    messages.append(content)
        elif event_type == "result":
            data = payload.get("result")
            if isinstance(data, str) and data:
                messages.append(data)
        return messages

    @staticmethod
    def _text_deltas(payload: dict[str, object]) -> list[tuple[int, str]]:
        if payload.get("type") != "content_block_delta":
            return []
        delta = payload.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return []
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return []
        index = payload.get("index", 0)
        if not isinstance(index, int):
            index = 0
        return [(index, text)]

    @staticmethod
    def _errors(payload: dict[str, object]) -> list[str]:
        if payload.get("type") != "error":
            return []
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return [data["message"]]
        message = payload.get("message")
        if isinstance(message, str) and message:
            return [message]
        return []

    @staticmethod
    def _unwrap_stream_event(payload: dict[str, object]) -> dict[str, object]:
        event = payload.get("event")
        if isinstance(event, dict):
            return event
        return payload

    @staticmethod
    def _live_events(payload: dict[str, object]) -> list[LiveEvent]:
        events: list[LiveEvent] = []
        event_type = payload.get("type")
        if event_type == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    events.append(
                        LiveEvent(kind="message", engine="claude", role="assistant", content=text)
                    )
        elif event_type == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_name = block.get("name")
                if isinstance(tool_name, str):
                    tool_input = block.get("input")
                    events.append(
                        LiveEvent(
                            kind="tool_call",
                            engine="claude",
                            role="assistant",
                            tool_name=tool_name,
                            tool_input=json.dumps(tool_input)
                            if isinstance(tool_input, (dict, list))
                            else None,
                        )
                    )
        elif event_type == "tool_result":
            content = payload.get("content")
            tool_output = (
                content if isinstance(content, str) else json.dumps(content) if content else ""
            )
            events.append(
                LiveEvent(
                    kind="tool_result",
                    engine="claude",
                    role="user",
                    tool_output=tool_output,
                )
            )
        elif event_type == "assistant":
            data = payload.get("message")
            if isinstance(data, dict):
                content = data.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if isinstance(text, str) and text:
                                events.append(
                                    LiveEvent(
                                        kind="message", engine="claude", role="assistant", content=text
                                    )
                                )
        elif event_type == "result":
            data = payload.get("result")
            if isinstance(data, str) and data:
                events.append(
                    LiveEvent(kind="message", engine="claude", role="assistant", content=data)
                )
            if payload.get("usage"):
                usage = payload["usage"]
                if isinstance(usage, dict):
                    meta: dict[str, str | int | bool | None] = {}
                    for field in ("input_tokens", "output_tokens"):
                        raw = usage.get(field)
                        if isinstance(raw, int):
                            meta[field] = raw
                    events.append(LiveEvent(kind="usage", engine="claude", metadata=meta))
        elif event_type == "error":
            data = payload.get("data")
            message = ""
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                message = data["message"]
            elif isinstance(payload.get("message"), str):
                message = payload["message"]
            if message:
                events.append(LiveEvent(kind="error", engine="claude", error=message))
        return events


_CLAUDE_TEXT_DELTA_RE = re.compile(
    r'"type"\s*:\s*"content_block_delta".*?"index"\s*:\s*(\d+).*?'
    r'"delta"\s*:\s*\{.*?"type"\s*:\s*"text_delta".*?"text"\s*:\s*"((?:\\.|[^"\\])*)"',
)


def _extract_claude_text_delta_fallback(stdout: str) -> list[str]:
    partial_blocks: dict[int, list[str]] = {}
    for line_idx, raw_line in enumerate(stdout.splitlines(), 1):
        match = _CLAUDE_TEXT_DELTA_RE.search(raw_line)
        if match is None:
            continue
        try:
            index = int(match.group(1))
            text = _decode_json_string_literal(match.group(2))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "claude delta fallback: failed to decode delta at line %d: %s (content: %.200s)",
                line_idx,
                exc,
                raw_line,
            )
            continue
        if text:
            partial_blocks.setdefault(index, []).append(text)
    return ["".join(partial_blocks[index]) for index in sorted(partial_blocks)]


def _decode_json_string_literal(value: str) -> str:
    return json.loads(f'"{value}"')


def _claude_usage_window(
    payload: dict[str, object],
    metadata: dict[str, str | int | bool | None],
) -> EngineUsageWindow | None:
    usage_payload = payload.get("usage")
    if not isinstance(usage_payload, dict):
        return None

    total_tokens = 0
    saw_tokens = False
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        raw_value = usage_payload.get(field)
        if isinstance(raw_value, int):
            metadata[field] = raw_value
            total_tokens += raw_value
            saw_tokens = True
    server_tool_use = usage_payload.get("server_tool_use")
    if isinstance(server_tool_use, dict):
        for field in ("web_search_requests", "web_fetch_requests"):
            raw_value = server_tool_use.get(field)
            if isinstance(raw_value, int):
                metadata[field] = raw_value
    cache_creation = usage_payload.get("cache_creation")
    if isinstance(cache_creation, dict):
        for field in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
            raw_value = cache_creation.get(field)
            if isinstance(raw_value, int):
                metadata[field] = raw_value
    service_tier = usage_payload.get("service_tier")
    if isinstance(service_tier, str) and service_tier:
        metadata["service_tier"] = service_tier
    total_cost_usd = payload.get("total_cost_usd")
    if isinstance(total_cost_usd, (int, float)):
        metadata["total_cost_usd"] = f"{total_cost_usd:.6f}"
    duration_ms = payload.get("duration_ms")
    if isinstance(duration_ms, int):
        metadata["duration_ms"] = duration_ms
    return EngineUsageWindow(used=total_tokens, unit="tokens") if saw_tokens else None


def _claude_error_details(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, str | int | bool | None]]:
    raw_error: object | None = None
    if payload.get("type") == "error":
        raw_error = payload.get("error")
        if raw_error is None:
            raw_error = payload.get("data")
    elif payload.get("type") == "result" and payload.get("is_error"):
        raw_error = payload.get("error")

    if raw_error is None:
        return None, {}

    metadata: dict[str, str | int | bool | None] = {}
    message = _claude_error_message(raw_error)
    nested = _decode_json_object(raw_error)
    if isinstance(nested, dict):
        error_type = nested.get("type")
        if isinstance(error_type, str) and error_type:
            metadata["error_type"] = error_type
        error_code = nested.get("code")
        if isinstance(error_code, str) and error_code:
            metadata["error_code"] = error_code
    if message:
        metadata["error_message"] = message
    return message, metadata


def _claude_error_message(raw_error: object) -> str | None:
    if isinstance(raw_error, str):
        nested = _decode_json_object(raw_error)
        if isinstance(nested, dict):
            return _claude_error_message(nested)
        message = raw_error.strip()
        return message or None
    if isinstance(raw_error, dict):
        nested_error = raw_error.get("error")
        if isinstance(nested_error, dict):
            nested_message = _claude_error_message(nested_error)
            if nested_message:
                return nested_message
        message = raw_error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None
