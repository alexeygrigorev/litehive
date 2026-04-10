import json
import re

from heru.adapters.common import _decode_json_object
from heru.base import StreamEventAdapter
from heru.types import EngineUsageWindow, LiveEvent, RuntimeEngineContinuation


def gemini_usage_window(
    payload: dict[str, object],
    metadata: dict[str, str | int | bool | None],
) -> EngineUsageWindow | None:
    event_type = str(payload.get("type", "")).lower()
    if event_type == "finished":
        value = payload.get("value")
        if not isinstance(value, dict):
            return None
        usage_metadata = value.get("usageMetadata")
        if not isinstance(usage_metadata, dict):
            return None
        for field in (
            "promptTokenCount",
            "candidatesTokenCount",
            "thoughtsTokenCount",
            "cachedContentTokenCount",
            "toolUsePromptTokenCount",
        ):
            raw_value = usage_metadata.get(field)
            if isinstance(raw_value, int):
                metadata[field] = raw_value
        reason = value.get("reason")
        if isinstance(reason, str) and reason:
            metadata["finish_reason"] = reason
        total_token_count = usage_metadata.get("totalTokenCount")
        return (
            EngineUsageWindow(used=total_token_count, unit="tokens")
            if isinstance(total_token_count, int)
            else None
        )
    if event_type != "result":
        return None
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None
    total_tokens = stats.get("total_tokens")
    input_tokens = stats.get("input_tokens")
    output_tokens = stats.get("output_tokens")
    cached_tokens = stats.get("cached")
    duration_ms = stats.get("duration_ms")
    if isinstance(total_tokens, int):
        metadata["total_tokens"] = total_tokens
    if isinstance(input_tokens, int):
        metadata["input_tokens"] = input_tokens
    if isinstance(output_tokens, int):
        metadata["output_tokens"] = output_tokens
    if isinstance(cached_tokens, int):
        metadata["cached_tokens"] = cached_tokens
    if isinstance(duration_ms, int):
        metadata["duration_ms"] = duration_ms
    if isinstance(total_tokens, int):
        return EngineUsageWindow(used=total_tokens, unit="tokens")
    token_parts = [value for value in (input_tokens, output_tokens) if isinstance(value, int)]
    return EngineUsageWindow(used=sum(token_parts), unit="tokens") if token_parts else None


def gemini_error_details(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, str | int | bool | None], EngineUsageWindow | None]:
    event_type = str(payload.get("type", "")).lower()
    raw_error: object | None = None
    if event_type == "error":
        raw_error = payload.get("value") or payload.get("data") or payload.get("error") or payload.get("message")
    elif event_type == "result" and payload.get("status") == "error":
        raw_error = payload.get("error") or payload.get("data") or payload.get("message")
    if raw_error is None:
        return None, {}, None
    metadata: dict[str, str | int | bool | None] = {}
    message = gemini_error_message(raw_error)
    usage = gemini_error_usage(raw_error, metadata)
    gemini_error_metadata(raw_error, metadata)
    if message:
        metadata["error_message"] = message
    return message, metadata, usage


def gemini_continuation(payload: dict[str, object]) -> RuntimeEngineContinuation | None:
    if payload.get("type") != "init":
        return None
    session_id = payload.get("session_id")
    model = payload.get("model")
    metadata: dict[str, str | int | bool | None] = {}
    if isinstance(model, str) and model:
        metadata["model"] = model
    return (
        RuntimeEngineContinuation(session_id=session_id, metadata=metadata)
        if isinstance(session_id, str) and session_id
        else None
    )


def gemini_stream_event_adapter() -> StreamEventAdapter:
    return StreamEventAdapter(live_events=live_events)


def live_events(payload: dict[str, object]) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    event_type = str(payload.get("type", "")).lower()
    if event_type == "content":
        text = payload.get("text")
        if isinstance(text, str) and text:
            events.append(LiveEvent(kind="message", engine="gemini", role="assistant", content=text))
    elif event_type == "tool_call":
        tool_name = payload.get("name")
        tool_input = payload.get("args")
        events.append(
            LiveEvent(
                kind="tool_call",
                engine="gemini",
                role="assistant",
                tool_name=tool_name if isinstance(tool_name, str) else None,
                tool_input=json.dumps(tool_input) if isinstance(tool_input, (dict, list)) else None,
            )
        )
    elif event_type == "tool_result":
        result = payload.get("result")
        events.append(
            LiveEvent(
                kind="tool_result",
                engine="gemini",
                role="user",
                tool_output=result if isinstance(result, str) else json.dumps(result) if result else None,
            )
        )
    elif event_type == "finished":
        value = payload.get("value")
        if isinstance(value, dict):
            usage_metadata = value.get("usageMetadata")
            if isinstance(usage_metadata, dict):
                meta: dict[str, str | int | bool | None] = {}
                for field in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount"):
                    raw = usage_metadata.get(field)
                    if isinstance(raw, int):
                        meta[field] = raw
                if meta:
                    events.append(LiveEvent(kind="usage", engine="gemini", metadata=meta))
    elif event_type == "error":
        raw_error = payload.get("value") or payload.get("error") or payload.get("message")
        message = raw_error.strip() if isinstance(raw_error, str) else raw_error.get("message") if isinstance(raw_error, dict) else None
        if isinstance(message, str) and message:
            events.append(LiveEvent(kind="error", engine="gemini", error=message))
    return events


def gemini_error_usage(
    raw_error: object,
    metadata: dict[str, str | int | bool | None],
) -> EngineUsageWindow | None:
    nested = _decode_json_object(raw_error)
    if not isinstance(nested, dict):
        return None
    details = nested.get("details")
    if not isinstance(details, list):
        return None
    limit: int | None = None
    unit: str | None = None
    reset_at: str | None = None
    remaining: int | None = None
    for raw_detail in details:
        if not isinstance(raw_detail, dict):
            continue
        detail_type = raw_detail.get("@type")
        if detail_type == "type.googleapis.com/google.rpc.QuotaFailure":
            violations = raw_detail.get("violations")
            if not isinstance(violations, list):
                continue
            for raw_violation in violations:
                if not isinstance(raw_violation, dict):
                    continue
                quota_value = raw_violation.get("quotaValue")
                if isinstance(quota_value, int):
                    limit = quota_value
                elif isinstance(quota_value, str) and quota_value.isdigit():
                    limit = int(quota_value)
                limit_hint = " ".join(
                    value.lower()
                    for value in (raw_violation.get("quotaMetric"), raw_violation.get("quotaId"))
                    if isinstance(value, str) and value
                )
                if "token" in limit_hint:
                    unit = "tokens"
                elif any(marker in limit_hint for marker in ("request", "rpm", "day")):
                    unit = "requests"
                break
        if detail_type == "type.googleapis.com/google.rpc.ErrorInfo":
            info_metadata = raw_detail.get("metadata")
            if isinstance(info_metadata, dict):
                quota_reset = info_metadata.get("quotaResetTimeStamp")
                if isinstance(quota_reset, str) and quota_reset:
                    reset_at = quota_reset
            reason = raw_detail.get("reason")
            if isinstance(reason, str) and reason in {"QUOTA_EXHAUSTED", "INSUFFICIENT_G1_CREDITS_BALANCE"}:
                remaining = 0
    if limit is None and unit is None and reset_at is None and remaining is None:
        return None
    metadata.setdefault("quota_limit", limit)
    metadata.setdefault("quota_reset_at", reset_at)
    return EngineUsageWindow(
        limit=limit,
        remaining=remaining,
        unit=unit or "requests",
        reset_at=reset_at,
    )


def gemini_error_metadata(raw_error: object, metadata: dict[str, str | int | bool | None]) -> None:
    nested = _decode_json_object(raw_error)
    if not isinstance(nested, dict):
        return
    raw_code = nested.get("code")
    if isinstance(raw_code, int):
        metadata["error_code"] = raw_code
    raw_status = nested.get("status")
    if isinstance(raw_status, str) and raw_status:
        metadata["error_status"] = raw_status
    reason = nested.get("reason")
    if isinstance(reason, str) and reason:
        metadata["error_reason"] = reason
    details = nested.get("details")
    if isinstance(details, list):
        for raw_detail in details:
            if not isinstance(raw_detail, dict):
                continue
            detail_type = raw_detail.get("@type")
            if detail_type == "type.googleapis.com/google.rpc.RetryInfo":
                retry_delay = raw_detail.get("retryDelay")
                if isinstance(retry_delay, str) and retry_delay:
                    metadata["retry_delay"] = retry_delay
                    retry_delay_ms = duration_to_millis(retry_delay)
                    if retry_delay_ms is not None:
                        metadata["retry_delay_ms"] = retry_delay_ms
            elif detail_type == "type.googleapis.com/google.rpc.ErrorInfo":
                domain = raw_detail.get("domain")
                if isinstance(domain, str) and domain:
                    metadata["error_domain"] = domain
                detail_reason = raw_detail.get("reason")
                if isinstance(detail_reason, str) and detail_reason:
                    metadata["error_reason"] = detail_reason
                detail_metadata = raw_detail.get("metadata")
                if isinstance(detail_metadata, dict):
                    for field, key in (
                        ("quota_limit_name", "quota_limit"),
                        ("quota_reset_at", "quotaResetTimeStamp"),
                        ("quota_reset_delay", "quotaResetDelay"),
                    ):
                        value = detail_metadata.get(key)
                        if isinstance(value, str) and value:
                            metadata[field] = value
            elif detail_type == "type.googleapis.com/google.rpc.QuotaFailure":
                violations = raw_detail.get("violations")
                if not isinstance(violations, list):
                    continue
                for raw_violation in violations:
                    if not isinstance(raw_violation, dict):
                        continue
                    for field, key in (("quota_metric", "quotaMetric"), ("quota_id", "quotaId")):
                        value = raw_violation.get(key)
                        if isinstance(value, str) and value:
                            metadata[field] = value
                    quota_dimensions = raw_violation.get("quotaDimensions")
                    if isinstance(quota_dimensions, dict):
                        model = quota_dimensions.get("model")
                        if isinstance(model, str) and model:
                            metadata["quota_model"] = model
                    break
    nested_error = nested.get("error")
    if nested_error is not None:
        gemini_error_metadata(nested_error, metadata)


def gemini_error_message(raw_error: object) -> str | None:
    if isinstance(raw_error, str):
        nested = _decode_json_object(raw_error)
        if isinstance(nested, dict):
            return gemini_error_message(nested)
        message = raw_error.strip()
        return message or None
    if isinstance(raw_error, dict):
        nested_error = raw_error.get("error")
        if isinstance(nested_error, dict):
            nested_message = gemini_error_message(nested_error)
            if nested_message:
                return nested_message
        message = raw_error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def duration_to_millis(value: str) -> int | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s)", value.strip())
    if match is None:
        return None
    magnitude = float(match.group(1))
    return int(magnitude if match.group(2) == "ms" else magnitude * 1000)
