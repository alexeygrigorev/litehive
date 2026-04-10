from heru.base import StreamEventAdapter
from heru.types import EngineUsageWindow, LiveEvent


def copilot_usage_observation(
    payload: dict[str, object],
) -> tuple[EngineUsageWindow | None, dict[str, str | int | bool | None]] | None:
    if payload.get("type") != "assistant.usage":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    metadata: dict[str, str | int | bool | None] = {}
    total_tokens = 0
    saw_tokens = False
    for field in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
        raw_value = data.get(field)
        if isinstance(raw_value, int):
            metadata[field] = raw_value
            total_tokens += raw_value
            saw_tokens = True
    if isinstance(data.get("model"), str) and data["model"]:
        metadata["model"] = data["model"]
    if isinstance(data.get("cost"), (int, float)):
        metadata["cost"] = str(data["cost"])
    usage = EngineUsageWindow(used=total_tokens, unit="tokens") if saw_tokens else None
    quota_snapshots = data.get("quotaSnapshots")
    if isinstance(quota_snapshots, dict) and quota_snapshots:
        selected_name, selected_snapshot = select_copilot_quota_snapshot(quota_snapshots)
        if isinstance(selected_snapshot, dict):
            quota_usage = copilot_quota_usage_window(selected_snapshot)
            if quota_usage is not None:
                usage = quota_usage
            metadata["quota_snapshot"] = selected_name
            for field in (
                "isUnlimitedEntitlement",
                "entitlementRequests",
                "usedRequests",
                "usageAllowedWithExhaustedQuota",
                "overage",
                "overageAllowedWithExhaustedQuota",
                "remainingPercentage",
            ):
                raw_value = selected_snapshot.get(field)
                if isinstance(raw_value, (bool, int)):
                    metadata[field] = raw_value
                elif isinstance(raw_value, float):
                    metadata[field] = f"{raw_value:.6f}"
            reset_date = selected_snapshot.get("resetDate")
            if isinstance(reset_date, str) and reset_date:
                metadata["resetDate"] = reset_date
    return usage, metadata


def copilot_stream_event_adapter() -> StreamEventAdapter:
    return StreamEventAdapter(
        final_messages=final_messages,
        text_deltas=text_deltas,
        errors=errors,
        live_events=live_events,
    )


def live_events(payload: dict[str, object]) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    event_type = payload.get("type")
    data = payload.get("data")
    if event_type == "assistant.message":
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, str) and content:
            events.append(LiveEvent(kind="message", engine="copilot", role="assistant", content=content))
    elif event_type == "assistant.message_delta":
        content = data.get("deltaContent") if isinstance(data, dict) else None
        if isinstance(content, str) and content:
            events.append(LiveEvent(kind="message", engine="copilot", role="assistant", content=content))
    elif event_type == "tool.execution_start" and isinstance(data, dict):
        tool_name = data.get("toolName") or data.get("tool")
        events.append(
            LiveEvent(
                kind="tool_call",
                engine="copilot",
                role="assistant",
                tool_name=tool_name if isinstance(tool_name, str) else None,
            )
        )
    elif event_type == "tool.execution_complete" and isinstance(data, dict):
        tool_name = data.get("toolName") or data.get("tool")
        result = data.get("result")
        tool_output = result if isinstance(result, str) else None
        if isinstance(result, dict):
            content = result.get("content") or result.get("detailedContent")
            if isinstance(content, str):
                tool_output = content
        events.append(
            LiveEvent(
                kind="tool_result",
                engine="copilot",
                role="user",
                tool_name=tool_name if isinstance(tool_name, str) else None,
                tool_output=tool_output,
            )
        )
    elif event_type == "assistant.usage" and isinstance(data, dict):
        meta: dict[str, str | int | bool | None] = {}
        for field in ("inputTokens", "outputTokens"):
            raw = data.get(field)
            if isinstance(raw, int):
                meta[field] = raw
        model = data.get("model")
        if isinstance(model, str):
            meta["model"] = model
        if meta:
            events.append(LiveEvent(kind="usage", engine="copilot", metadata=meta))
    elif event_type == "error" and isinstance(data, dict) and isinstance(data.get("message"), str):
        events.append(LiveEvent(kind="error", engine="copilot", error=data["message"]))
    return events


def final_messages(payload: dict[str, object]) -> list[str]:
    if payload.get("type") != "assistant.message":
        return []
    data = payload.get("data")
    content = data.get("content") if isinstance(data, dict) else None
    return [content] if isinstance(content, str) and content else []


def text_deltas(payload: dict[str, object]) -> list[tuple[int, str]]:
    if payload.get("type") != "assistant.message_delta":
        return []
    data = payload.get("data")
    content = data.get("deltaContent") if isinstance(data, dict) else None
    return [(0, content)] if isinstance(content, str) and content else []


def errors(payload: dict[str, object]) -> list[str]:
    event_type = payload.get("type")
    if event_type == "error":
        data = payload.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        return [message] if isinstance(message, str) and message else []
    if event_type != "tool.execution_complete":
        return []
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("success", True):
        return []
    result = data.get("result")
    if isinstance(result, dict):
        content = result.get("content") or result.get("detailedContent")
        return [content] if isinstance(content, str) and content else []
    return [result] if isinstance(result, str) and result else []


def copilot_quota_usage_window(snapshot: dict[str, object]) -> EngineUsageWindow | None:
    entitlement_requests = snapshot.get("entitlementRequests")
    used_requests = snapshot.get("usedRequests")
    if not isinstance(used_requests, int):
        return None
    remaining = entitlement_requests - used_requests if isinstance(entitlement_requests, int) else None
    reset_at = snapshot.get("resetDate")
    return EngineUsageWindow(
        used=used_requests,
        limit=entitlement_requests if isinstance(entitlement_requests, int) else None,
        remaining=remaining,
        unit="requests",
        reset_at=reset_at if isinstance(reset_at, str) and reset_at else None,
    )


def select_copilot_quota_snapshot(quota_snapshots: dict[str, object]) -> tuple[str, object]:
    for preferred in ("chat", "premium", "requests"):
        if preferred in quota_snapshots:
            return preferred, quota_snapshots[preferred]
    name = next(iter(quota_snapshots))
    return name, quota_snapshots[name]
