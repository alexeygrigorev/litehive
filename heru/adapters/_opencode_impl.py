import logging

from heru.base import StreamEventAdapter, iter_jsonl_payloads
from heru.types import EngineUsageWindow, LiveEvent, RuntimeEngineContinuation


logger = logging.getLogger("litehive.agents.adapters.opencode")


def extract_opencode_transcript(stdout: str) -> str:
    messages: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") != "text":
            continue
        part = payload.get("part")
        if not isinstance(part, dict):
            logger.warning(
                "opencode: text event has non-dict part (type=%s, content: %.200s)",
                type(part).__name__,
                str(part),
            )
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            messages.append(text)
        else:
            logger.warning("opencode: text event part has no text field (keys=%s)", list(part.keys()))
    return "\n".join(part.rstrip() for part in messages if part.strip()).strip()


def extract_opencode_errors(stdout: str) -> str:
    errors: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        error_message, _ = opencode_error_details(payload)
        if error_message:
            errors.append(error_message)
    return "\n".join(errors).strip()


def opencode_usage_window(
    payload: dict[str, object],
    metadata: dict[str, str | int | bool | None],
) -> EngineUsageWindow | None:
    if payload.get("type") != "step_finish":
        return None
    part = payload.get("part")
    if not isinstance(part, dict):
        return None
    tokens = part.get("tokens")
    if not isinstance(tokens, dict):
        return None
    total_tokens = tokens.get("total")
    input_tokens = tokens.get("input")
    output_tokens = tokens.get("output")
    reasoning_tokens = tokens.get("reasoning")
    if isinstance(total_tokens, int):
        metadata["total_tokens"] = total_tokens
    if isinstance(input_tokens, int):
        metadata["input_tokens"] = input_tokens
    if isinstance(output_tokens, int):
        metadata["output_tokens"] = output_tokens
    if isinstance(reasoning_tokens, int):
        metadata["reasoning_tokens"] = reasoning_tokens
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        cache_read = cache.get("read")
        cache_write = cache.get("write")
        if isinstance(cache_read, int):
            metadata["cache_read_tokens"] = cache_read
        if isinstance(cache_write, int):
            metadata["cache_write_tokens"] = cache_write
    cost = part.get("cost")
    if isinstance(cost, (int, float)):
        metadata["cost"] = f"{cost:.6f}"
    reason = part.get("reason")
    if isinstance(reason, str) and reason:
        metadata["finish_reason"] = reason
    used_tokens = total_tokens
    if not isinstance(used_tokens, int):
        token_parts = [value for value in (input_tokens, output_tokens) if isinstance(value, int)]
        used_tokens = sum(token_parts) if token_parts else None
    return EngineUsageWindow(used=used_tokens, unit="tokens") if isinstance(used_tokens, int) else None


def opencode_error_details(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, str | int | bool | None]]:
    if payload.get("type") != "error":
        return None, {}
    raw_error = payload.get("error")
    if not isinstance(raw_error, dict):
        return None, {}
    metadata: dict[str, str | int | bool | None] = {}
    name = raw_error.get("name")
    if isinstance(name, str) and name:
        metadata["error_name"] = name
    data = raw_error.get("data")
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            metadata["error_message"] = message.strip()
            for field in ("status", "code", "type"):
                raw_value = data.get(field)
                if isinstance(raw_value, (str, int)):
                    metadata[f"error_{field}"] = raw_value
            return message.strip(), metadata
    return (name.strip(), metadata) if isinstance(name, str) and name.strip() else (None, metadata)


def opencode_continuation(payload: dict[str, object]) -> RuntimeEngineContinuation | None:
    session_id = payload.get("sessionID")
    return RuntimeEngineContinuation(session_id=session_id) if isinstance(session_id, str) and session_id else None


def opencode_stream_event_adapter() -> StreamEventAdapter:
    return StreamEventAdapter(live_events=live_events, continuation_id=opencode_continuation_id)


def opencode_continuation_id(payload: dict[str, object]) -> str | None:
    continuation = opencode_continuation(payload)
    return continuation.resume_id if continuation is not None else None


def live_events(payload: dict[str, object]) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    event_type = payload.get("type")
    if event_type == "text":
        part = payload.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                events.append(
                    LiveEvent(kind="message", engine="opencode", role="assistant", content=text)
                )
    elif event_type == "step_finish":
        part = payload.get("part")
        if isinstance(part, dict):
            tokens = part.get("tokens")
            meta: dict[str, str | int | bool | None] = {}
            if isinstance(tokens, dict):
                for field in ("total", "input", "output", "reasoning"):
                    raw = tokens.get(field)
                    if isinstance(raw, int):
                        meta[f"{field}_tokens"] = raw
            cost = part.get("cost")
            if isinstance(cost, (int, float)):
                meta["cost"] = f"{cost:.6f}"
            if meta:
                events.append(LiveEvent(kind="usage", engine="opencode", metadata=meta))
    elif event_type == "error":
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            data = raw_error.get("data")
            message = data.get("message") if isinstance(data, dict) else raw_error.get("name")
            if isinstance(message, str) and message.strip():
                events.append(
                    LiveEvent(kind="error", engine="opencode", error=message.strip())
                )
    return events
