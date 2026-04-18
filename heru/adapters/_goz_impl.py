import json
import logging

from heru.base import StreamEventAdapter, iter_jsonl_payloads
from heru.types import EngineUsageWindow, LiveEvent, RuntimeEngineContinuation


logger = logging.getLogger("litehive.agents.adapters.goz")


def goz_session_id(payload: dict[str, object]) -> str | None:
    for key in ("sessionID", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    part = payload.get("part")
    if isinstance(part, dict):
        for key in ("sessionID", "session_id"):
            value = part.get(key)
            if isinstance(value, str) and value:
                return value
        continuation = part.get("continuation")
        if isinstance(continuation, dict):
            resume = continuation.get("resume_session_id") or continuation.get("resumeSessionId")
            if isinstance(resume, str) and resume:
                return resume
    return None


def goz_continuation(payload: dict[str, object]) -> RuntimeEngineContinuation | None:
    if str(payload.get("type", "")).lower() != "step_finish":
        return None
    session_id = goz_session_id(payload)
    return RuntimeEngineContinuation(session_id=session_id) if session_id else None


def extract_goz_transcript(stdout: str) -> str:
    sections: list[str] = []
    current_text: list[str] = []

    def flush_text() -> None:
        if current_text:
            joined = "".join(current_text).strip()
            if joined:
                sections.append(joined)
            current_text.clear()

    for payload in iter_jsonl_payloads(stdout):
        event_type = str(payload.get("type", "")).lower()
        if event_type == "text":
            part = payload.get("part")
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    current_text.append(text)
                    continue
        if event_type == "tool_use":
            flush_text()
            part = payload.get("part")
            if isinstance(part, dict):
                tool_block = format_goz_tool_block(part)
                if tool_block:
                    sections.append(tool_block)
            continue
        flush_text()
        text = goz_message_text(payload)
        if text:
            sections.append(text)
    flush_text()
    return "\n\n".join(sections).strip()


def extract_goz_errors(stdout: str) -> list[str]:
    errors: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        error_message, _ = goz_error_details(payload)
        if error_message:
            errors.append(error_message)
    return errors


def goz_usage_window(
    payload: dict[str, object],
    metadata: dict[str, str | int | bool | None],
) -> EngineUsageWindow | None:
    usage_payload = goz_usage_payload(payload)
    if usage_payload is None:
        return None
    input_tokens = goz_usage_int(usage_payload, "input_tokens", "inputTokens", "prompt_tokens")
    output_tokens = goz_usage_int(usage_payload, "output_tokens", "outputTokens", "completion_tokens")
    total_tokens = goz_usage_int(usage_payload, "total_tokens", "totalTokens")
    if isinstance(input_tokens, int):
        metadata["input_tokens"] = input_tokens
    if isinstance(output_tokens, int):
        metadata["output_tokens"] = output_tokens
    if isinstance(total_tokens, int):
        metadata["total_tokens"] = total_tokens
    model = usage_payload.get("model") or payload.get("model")
    if isinstance(model, str) and model:
        metadata["model"] = model
    cost = goz_cost_value(payload, usage_payload)
    if cost is not None:
        metadata["cost"] = f"{cost:.6f}"
    used_tokens = total_tokens
    if not isinstance(used_tokens, int):
        token_parts = [value for value in (input_tokens, output_tokens) if isinstance(value, int)]
        used_tokens = sum(token_parts) if token_parts else None
    return EngineUsageWindow(used=used_tokens, unit="tokens") if isinstance(used_tokens, int) else None


def goz_error_details(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, str | int | bool | None]]:
    raw_error: object | None = None
    event_type = str(payload.get("type", "")).lower()
    if event_type == "error":
        raw_error = payload.get("error") or payload.get("data") or payload.get("message")
    elif payload.get("status") == "error":
        raw_error = payload.get("error") or payload.get("message") or payload.get("data")
    if raw_error is None:
        return None, {}
    metadata: dict[str, str | int | bool | None] = {}
    if isinstance(raw_error, dict):
        error_type = raw_error.get("type") or raw_error.get("name")
        if isinstance(error_type, str) and error_type:
            metadata["error_type"] = error_type
        error_code = raw_error.get("code")
        if isinstance(error_code, (str, int)):
            metadata["error_code"] = error_code
    message = goz_error_message(raw_error)
    if message:
        metadata["error_message"] = message
    return message, metadata


def goz_stream_event_adapter() -> StreamEventAdapter:
    return StreamEventAdapter(live_events=live_events, continuation_id=goz_continuation_id)


def goz_continuation_id(payload: dict[str, object]) -> str | None:
    continuation = goz_continuation(payload)
    return continuation.resume_id if continuation is not None else None


def live_events(payload: dict[str, object]) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    event_type = str(payload.get("type", "")).lower()
    text = goz_message_text(payload)
    if text:
        events.append(LiveEvent(kind="message", engine="goz", role="assistant", content=text))
    if event_type in {"tool_call", "tool.call"}:
        tool_name = payload.get("name") or payload.get("tool_name") or payload.get("tool")
        raw_input = payload.get("input") or payload.get("arguments") or payload.get("args")
        events.append(
            LiveEvent(
                kind="tool_call",
                engine="goz",
                role="assistant",
                tool_name=tool_name if isinstance(tool_name, str) else None,
                tool_input=json.dumps(raw_input) if isinstance(raw_input, (dict, list)) else raw_input if isinstance(raw_input, str) else None,
            )
        )
    if event_type in {"tool_result", "tool.result"}:
        tool_name = payload.get("name") or payload.get("tool_name") or payload.get("tool")
        raw_output = payload.get("output") or payload.get("result")
        events.append(
            LiveEvent(
                kind="tool_result",
                engine="goz",
                role="user",
                tool_name=tool_name if isinstance(tool_name, str) else None,
                tool_output=json.dumps(raw_output) if isinstance(raw_output, (dict, list)) else raw_output if isinstance(raw_output, str) else None,
            )
        )
    meta: dict[str, str | int | bool | None] = {}
    usage = goz_usage_window(payload, meta)
    if usage is not None:
        events.append(LiveEvent(kind="usage", engine="goz", metadata=meta))
    error_message, _ = goz_error_details(payload)
    if error_message:
        events.append(LiveEvent(kind="error", engine="goz", error=error_message))
    return events


def format_goz_tool_block(part: dict[str, object]) -> str | None:
    lines = ["```tool"]
    name = part.get("name", "")
    id_ = part.get("id", "")
    if name:
        lines.append(f"name: {name}")
    if id_:
        lines.append(f"id: {id_}")
    input_ = part.get("input")
    if input_ is not None:
        lines.append("input:")
        lines.append(json.dumps(input_, indent=2))
    output = part.get("output")
    if output is not None:
        lines.append("error:" if bool(part.get("is_error", False)) else "output:")
        lines.append(str(output))
    lines.append("```")
    return "\n".join(lines)


def goz_message_text(payload: dict[str, object]) -> str | None:
    event_type = str(payload.get("type", "")).lower()
    if event_type in {"item.completed", "item.updated"}:
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            return text.strip() if isinstance(text, str) and text.strip() else None
    if event_type == "message" and payload.get("role") not in {None, "assistant"}:
        return None
    candidate: object | None = None
    if event_type in {"message", "assistant", "assistant.message", "assistant.message_delta", "message.delta", "content", "content.delta", "text", "text_delta"}:
        candidate = payload.get("text") or payload.get("delta") or payload.get("content") or payload.get("message") or payload.get("data") or payload.get("part")
    elif "message" in payload or "content" in payload:
        candidate = payload.get("message") or payload.get("content")
    text = goz_extract_text(candidate)
    return text.strip() if isinstance(text, str) and text.strip() else None


def goz_extract_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = "".join(part for part in (goz_extract_text(item) for item in value) if part)
        return text or None
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        delta = value.get("delta")
        if isinstance(delta, str):
            return delta
        for key in ("content", "message", "data", "result", "part"):
            extracted = goz_extract_text(value.get(key))
            if extracted:
                return extracted
        if value:
            logger.warning(
                "goz: _goz_extract_text could not extract text from dict with keys %s (content: %.200s)",
                list(value.keys()),
                json.dumps(value, default=str),
            )
    return None


def goz_usage_payload(payload: dict[str, object]) -> dict[str, object] | None:
    direct_usage = payload.get("usage")
    if isinstance(direct_usage, dict):
        return direct_usage
    for container_key in ("data", "result", "metrics"):
        container = payload.get(container_key)
        if not isinstance(container, dict):
            continue
        nested_usage = container.get("usage")
        if isinstance(nested_usage, dict):
            return nested_usage
        if any(key in container for key in ("input_tokens", "output_tokens", "total_tokens")):
            return container
    return payload if str(payload.get("type", "")).lower() == "usage" else None


def goz_usage_int(payload: dict[str, object], *keys: str) -> int | None:
    for key in keys:
        raw_value = payload.get(key)
        if isinstance(raw_value, int):
            return raw_value
    return None


def goz_cost_value(payload: dict[str, object], usage_payload: dict[str, object]) -> float | None:
    candidates: list[object] = [usage_payload.get("cost"), payload.get("cost"), payload.get("total_cost")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("cost"), data.get("total_cost")])
    for candidate in candidates:
        if isinstance(candidate, (int, float)):
            return float(candidate)
        if isinstance(candidate, dict):
            for key in ("total_usd", "usd", "amount"):
                raw_value = candidate.get(key)
                if isinstance(raw_value, (int, float)):
                    return float(raw_value)
    return None


def goz_error_message(raw_error: object) -> str | None:
    if isinstance(raw_error, str):
        message = raw_error.strip()
        return message or None
    if isinstance(raw_error, dict):
        nested_error = raw_error.get("error")
        if nested_error is not None:
            nested_message = goz_error_message(nested_error)
            if nested_message:
                return nested_message
        for key in ("message", "detail", "error"):
            value = raw_error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
