import logging
import re
import json
from dataclasses import dataclass

from heru.adapters.common import _decode_json_object, classify_execution_limit
from heru.base import StreamEventAdapter
from heru.types import EngineUsageWindow, LiveEvent, RuntimeEngineContinuation


logger = logging.getLogger("litehive.agents.adapters.codex")


@dataclass(frozen=True, slots=True)
class CodexUsageLimitResult:
    limit_reason: str
    retry_at: str | None = None
    purchase_more_credits: bool = False


_CODEX_USAGE_LIMIT_RE = re.compile(r"you['\u2019]ve hit your usage limit", re.IGNORECASE)


def classify_codex_usage_limit(text: str | None) -> CodexUsageLimitResult | None:
    if not text or not _CODEX_USAGE_LIMIT_RE.search(text):
        return None
    retry_at = codex_retry_at_hint(text)
    if retry_at:
        logger.info("Codex usage limit hit; engine available again at %s", retry_at)
    else:
        logger.info("Codex usage limit hit; no reset date available")
    return CodexUsageLimitResult(
        limit_reason="usage limit reached",
        retry_at=retry_at,
        purchase_more_credits="purchase more credits" in text.lower(),
    )


@dataclass(slots=True)
class _JsonBalance:
    braces: int = 0
    brackets: int = 0
    in_string: bool = False
    escaped: bool = False


def iter_codex_payloads(stdout: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    buffered_lines: list[str] = []
    buffer_start_line = 0
    balance = _JsonBalance()

    for line_number, raw_line in enumerate(stdout.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped and not buffered_lines:
            continue

        if not buffered_lines:
            payload = _decode_codex_payload(stripped)
            if payload is not None:
                payloads.append(payload)
                continue
            if stripped.startswith("{"):
                buffered_lines = [raw_line]
                buffer_start_line = line_number
                balance = _update_json_balance(_JsonBalance(), raw_line)
                if _json_buffer_is_complete(balance):
                    _warn_bad_codex_payload(buffer_start_line, stripped, "invalid JSON object")
                    buffered_lines = []
                continue
            _warn_bad_codex_payload(line_number, stripped, "invalid JSON object")
            continue

        buffered_lines.append(raw_line)
        balance = _update_json_balance(balance, raw_line)
        if not _json_buffer_is_complete(balance):
            continue

        payload_text = "\n".join(buffered_lines).strip()
        payload = _decode_codex_payload(payload_text)
        if payload is not None:
            payloads.append(payload)
        else:
            _warn_bad_codex_payload(buffer_start_line, payload_text, "invalid buffered JSON object")
        buffered_lines = []
        balance = _JsonBalance()

    if buffered_lines:
        payload_text = "\n".join(buffered_lines).strip()
        _warn_bad_codex_payload(buffer_start_line, payload_text, "unterminated JSON object")
    return payloads


def extract_codex_messages(stdout: str) -> str:
    messages: dict[str, str] = {}
    for payload in iter_codex_payloads(stdout):
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
    command_errors: dict[str, str] = {}
    for payload in iter_codex_payloads(stdout):
        event_type = payload.get("type")
        if event_type in {"error", "turn.failed"}:
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                errors.append(message.strip())
            continue
        if event_type not in {"item.completed", "item.updated"}:
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
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


def codex_usage_window(
    payload: dict[str, object],
    metadata: dict[str, str | int | bool | None],
) -> EngineUsageWindow | None:
    if payload.get("type") != "turn.completed":
        return None
    usage_payload = payload.get("usage")
    if not isinstance(usage_payload, dict):
        return None
    input_tokens = usage_payload.get("input_tokens")
    output_tokens = usage_payload.get("output_tokens")
    cached_input_tokens = usage_payload.get("cached_input_tokens")
    reasoning_tokens = usage_payload.get("reasoning_tokens")
    total_tokens = usage_payload.get("total_tokens")
    if isinstance(input_tokens, int):
        metadata["input_tokens"] = input_tokens
    if isinstance(output_tokens, int):
        metadata["output_tokens"] = output_tokens
    if isinstance(cached_input_tokens, int):
        metadata["cached_input_tokens"] = cached_input_tokens
    if isinstance(reasoning_tokens, int):
        metadata["reasoning_tokens"] = reasoning_tokens
    if isinstance(total_tokens, int):
        metadata["total_tokens"] = total_tokens
    used_tokens = total_tokens
    if not isinstance(used_tokens, int):
        token_parts = [value for value in (input_tokens, output_tokens) if isinstance(value, int)]
        used_tokens = sum(token_parts) if token_parts else None
    return EngineUsageWindow(used=used_tokens, unit="tokens") if isinstance(used_tokens, int) else None


def codex_error_details(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, str | int | bool | None]]:
    raw_error: object | None = None
    if payload.get("type") == "error":
        raw_error = payload.get("message")
    elif payload.get("type") == "turn.failed":
        raw_failure = payload.get("error")
        if isinstance(raw_failure, dict):
            raw_error = raw_failure.get("message")
    if raw_error is None:
        return None, {}
    metadata: dict[str, str | int | bool | None] = {}
    nested = _decode_json_object(raw_error)
    message = codex_error_message(raw_error)
    if isinstance(nested, dict):
        status = nested.get("status")
        if isinstance(status, int):
            metadata["error_status"] = status
        nested_error = nested.get("error")
        if isinstance(nested_error, dict):
            error_type = nested_error.get("type")
            if isinstance(error_type, str) and error_type:
                metadata["error_type"] = error_type
            error_code = nested_error.get("code")
            if isinstance(error_code, str) and error_code:
                metadata["error_code"] = error_code
    retry_at_hint = codex_retry_at_hint(message)
    if retry_at_hint:
        metadata["retry_at_hint"] = retry_at_hint
    if message and "purchase more credits" in message.lower():
        metadata["purchase_more_credits"] = True
    return message, metadata


def codex_stderr_limit(stderr: str, metadata: dict[str, str | int | bool | None]) -> str | None:
    codex_limit = classify_codex_usage_limit(stderr)
    if codex_limit:
        if codex_limit.retry_at:
            metadata.setdefault("retry_at_hint", codex_limit.retry_at)
        if codex_limit.purchase_more_credits:
            metadata.setdefault("purchase_more_credits", True)
        return codex_limit.limit_reason
    retry_at_hint = codex_retry_at_hint(stderr)
    if retry_at_hint:
        metadata.setdefault("retry_at_hint", retry_at_hint)
    if "purchase more credits" in stderr.lower():
        metadata.setdefault("purchase_more_credits", True)
    return classify_execution_limit(stderr)


def codex_continuation(payload: dict[str, object]) -> RuntimeEngineContinuation | None:
    if payload.get("type") != "thread.started":
        return None
    thread_id = payload.get("thread_id")
    return RuntimeEngineContinuation(thread_id=thread_id) if isinstance(thread_id, str) and thread_id else None


def codex_stream_event_adapter() -> StreamEventAdapter:
    return StreamEventAdapter(unwrap_event=unwrap_stream_event, live_events=codex_live_events)


def codex_live_events(payload: dict[str, object]) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    event_type = payload.get("type")
    if event_type == "item.completed":
        item = payload.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    events.append(
                        LiveEvent(kind="message", engine="codex", role="assistant", content=text)
                    )
            elif item_type == "command_execution":
                command = item.get("command")
                tool_name = str(command[0]) if isinstance(command, list) and command and command[0] else None
                aggregated = item.get("aggregated_output")
                exit_code = item.get("exit_code")
                events.append(
                    LiveEvent(
                        kind="tool_result",
                        engine="codex",
                        tool_name=tool_name,
                        tool_output=aggregated if isinstance(aggregated, str) else None,
                        metadata={"exit_code": exit_code} if isinstance(exit_code, int) else {},
                    )
                )
    elif event_type == "turn.completed":
        usage_payload = payload.get("usage")
        if isinstance(usage_payload, dict):
            meta: dict[str, str | int | bool | None] = {}
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                raw = usage_payload.get(field)
                if isinstance(raw, int):
                    meta[field] = raw
            events.append(LiveEvent(kind="usage", engine="codex", metadata=meta))
    elif event_type in {"error", "turn.failed"}:
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            events.append(LiveEvent(kind="error", engine="codex", error=message.strip()))
    return events


def unwrap_stream_event(payload: dict[str, object]) -> dict[str, object]:
    event = payload.get("event")
    return event if isinstance(event, dict) else payload


def codex_retry_at_hint(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"try again at\s+([^.;]+)", text, re.IGNORECASE)
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def codex_error_message(raw_error: object) -> str | None:
    if isinstance(raw_error, str):
        nested = _decode_json_object(raw_error)
        if isinstance(nested, dict):
            return codex_error_message(nested)
        message = raw_error.strip()
        return message or None
    if isinstance(raw_error, dict):
        nested_error = raw_error.get("error")
        if isinstance(nested_error, dict):
            nested_message = nested_error.get("message")
            if isinstance(nested_message, str) and nested_message.strip():
                return nested_message.strip()
        message = raw_error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def _decode_codex_payload(raw_payload: str) -> dict[str, object] | None:
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(payload, dict):
        return payload
    logger.warning(
        "codex: skipping non-object JSON payload (type=%s, content: %.200s)",
        type(payload).__name__,
        raw_payload,
    )
    return None


def _json_buffer_is_complete(balance: _JsonBalance) -> bool:
    return (
        not balance.in_string
        and not balance.escaped
        and balance.braces == 0
        and balance.brackets == 0
    )


def _update_json_balance(balance: _JsonBalance, text: str) -> _JsonBalance:
    for char in text:
        if balance.in_string:
            if balance.escaped:
                balance.escaped = False
                continue
            if char == "\\":
                balance.escaped = True
                continue
            if char == '"':
                balance.in_string = False
            continue
        if char == '"':
            balance.in_string = True
            continue
        if char == "{":
            balance.braces += 1
            continue
        if char == "}":
            balance.braces -= 1
            continue
        if char == "[":
            balance.brackets += 1
            continue
        if char == "]":
            balance.brackets -= 1
    return balance


def _warn_bad_codex_payload(line_number: int, content: str, reason: str) -> None:
    logger.warning(
        "codex: skipping %s at line %d (content: %.200s)",
        reason,
        line_number,
        content,
    )
