import json
import logging
import re

from heru.adapters.common import _decode_json_object
from heru.base import StreamEventAdapter
from heru.types import EngineUsageWindow, LiveEvent, RuntimeEngineContinuation


logger = logging.getLogger("litehive.agents.adapters.claude")

_CLAUDE_TEXT_DELTA_RE = re.compile(
    r'"type"\s*:\s*"content_block_delta".*?"index"\s*:\s*(\d+).*?'
    r'"delta"\s*:\s*\{.*?"type"\s*:\s*"text_delta".*?"text"\s*:\s*"((?:\\.|[^"\\])*)"'
)


def claude_stream_event_adapter() -> StreamEventAdapter:
    return StreamEventAdapter(
        unwrap_event=unwrap_stream_event,
        text_deltas=text_deltas,
        final_messages=final_messages,
        errors=errors,
        live_events=live_events,
        continuation_id=claude_continuation_id,
    )


def claude_continuation_id(payload: dict[str, object]) -> str | None:
    continuation = claude_continuation(payload)
    return continuation.resume_id if continuation is not None else None


def extract_claude_text_delta_fallback(stdout: str) -> list[str]:
    partial_blocks: dict[int, list[str]] = {}
    for line_idx, raw_line in enumerate(stdout.splitlines(), 1):
        match = _CLAUDE_TEXT_DELTA_RE.search(raw_line)
        if match is None:
            continue
        try:
            index = int(match.group(1))
            text = json.loads(f'"{match.group(2)}"')
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


def claude_usage_window(
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


def claude_error_details(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, str | int | bool | None]]:
    raw_error: object | None = None
    if payload.get("type") == "error":
        raw_error = payload.get("error") or payload.get("data")
    elif payload.get("type") == "result" and payload.get("is_error"):
        raw_error = payload.get("error")
    if raw_error is None:
        return None, {}
    metadata: dict[str, str | int | bool | None] = {}
    message = claude_error_message(raw_error)
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


def claude_continuation(payload: dict[str, object]) -> RuntimeEngineContinuation | None:
    if payload.get("type") != "system" or payload.get("subtype") != "init":
        return None
    session_id = payload.get("session_id")
    return RuntimeEngineContinuation(session_id=session_id) if isinstance(session_id, str) and session_id else None


def final_messages(payload: dict[str, object]) -> list[str]:
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


def text_deltas(payload: dict[str, object]) -> list[tuple[int, str]]:
    if payload.get("type") != "content_block_delta":
        return []
    delta = payload.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return []
    text = delta.get("text")
    index = payload.get("index", 0)
    if isinstance(text, str) and text:
        return [(index if isinstance(index, int) else 0, text)]
    return []


def errors(payload: dict[str, object]) -> list[str]:
    if payload.get("type") != "error":
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return [data["message"]]
    message = payload.get("message")
    return [message] if isinstance(message, str) and message else []


def unwrap_stream_event(payload: dict[str, object]) -> dict[str, object]:
    event = payload.get("event")
    return event if isinstance(event, dict) else payload


def live_events(payload: dict[str, object]) -> list[LiveEvent]:
    events_out: list[LiveEvent] = []
    event_type = payload.get("type")
    if event_type == "content_block_delta":
        delta = payload.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                events_out.append(
                    LiveEvent(kind="message", engine="claude", role="assistant", content=text)
                )
    elif event_type == "content_block_start":
        block = payload.get("content_block")
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_name = block.get("name")
            if isinstance(tool_name, str):
                tool_input = block.get("input")
                events_out.append(
                    LiveEvent(
                        kind="tool_call",
                        engine="claude",
                        role="assistant",
                        tool_name=tool_name,
                        tool_input=json.dumps(tool_input) if isinstance(tool_input, (dict, list)) else None,
                    )
                )
    elif event_type == "tool_result":
        content = payload.get("content")
        tool_output = content if isinstance(content, str) else json.dumps(content) if content else ""
        events_out.append(
            LiveEvent(kind="tool_result", engine="claude", role="user", tool_output=tool_output)
        )
    elif event_type == "assistant":
        for message in final_messages(payload):
            events_out.append(
                LiveEvent(kind="message", engine="claude", role="assistant", content=message)
            )
    elif event_type == "result":
        for message in final_messages(payload):
            events_out.append(
                LiveEvent(kind="message", engine="claude", role="assistant", content=message)
            )
        usage = payload.get("usage")
        if isinstance(usage, dict):
            meta: dict[str, str | int | bool | None] = {}
            for field in ("input_tokens", "output_tokens"):
                raw = usage.get(field)
                if isinstance(raw, int):
                    meta[field] = raw
            events_out.append(LiveEvent(kind="usage", engine="claude", metadata=meta))
    elif event_type == "error":
        data = payload.get("data")
        message = data["message"] if isinstance(data, dict) and isinstance(data.get("message"), str) else payload.get("message")
        if isinstance(message, str) and message:
            events_out.append(LiveEvent(kind="error", engine="claude", error=message))
    return events_out


def claude_error_message(raw_error: object) -> str | None:
    if isinstance(raw_error, str):
        nested = _decode_json_object(raw_error)
        if isinstance(nested, dict):
            return claude_error_message(nested)
        message = raw_error.strip()
        return message or None
    if isinstance(raw_error, dict):
        nested_error = raw_error.get("error")
        if isinstance(nested_error, dict):
            nested_message = claude_error_message(nested_error)
            if nested_message:
                return nested_message
        message = raw_error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None
