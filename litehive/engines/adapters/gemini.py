"""Gemini CLI engine adapter."""

import json
import re
from pathlib import Path

from litehive.engines.adapters.common import _decode_json_object, classify_execution_limit
from litehive.engines.base import (
    CLIExecutionResult,
    ExternalCLIAdapter,
    extract_jsonl_errors,
    extract_jsonl_messages,
    iter_jsonl_payloads,
    parse_stage_report_text,
)
from litehive.models import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
)


class GeminiCLIAdapter(ExternalCLIAdapter):
    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        command = [self.binary, "-p", prompt, "--output-format", "stream-json", "--yolo"]
        if model:
            command.extend(["-m", model])
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = extract_jsonl_messages(execution.stdout)
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
            stderr_lines = extract_jsonl_errors(execution.stdout)
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
        metadata: dict[str, str | int | bool | None] = {}
        usage: EngineUsageWindow | None = None
        limit_reason: str | None = None
        for payload in reversed(payloads):
            if usage is None:
                usage = _gemini_usage_window(payload, metadata)
            if limit_reason is None:
                error_message, error_metadata, error_usage = _gemini_error_details(payload)
                if error_metadata:
                    metadata.update(error_metadata)
                if usage is None and error_usage is not None:
                    usage = error_usage
                if error_message:
                    metadata.setdefault("error_message", error_message)
                    limit_reason = classify_execution_limit(error_message)
            if payload.get("type") == "init":
                model = payload.get("model")
                if isinstance(model, str) and model:
                    metadata.setdefault("model", model)
        if limit_reason is None and execution.stderr.strip():
            limit_reason = classify_execution_limit(execution.stderr)
        if usage is None and limit_reason is None and not metadata:
            return None
        return EngineUsageObservation(
            source="provider",
            provider="google",
            success=execution.exit_code == 0,
            limit_reason=limit_reason,
            usage=usage,
            metadata=metadata,
        )


def _gemini_live_events(payload: dict[str, object]) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    event_type = str(payload.get("type", "")).lower()
    if event_type == "content":
        text = payload.get("text")
        if isinstance(text, str) and text:
            events.append(
                LiveEvent(kind="message", engine="gemini", role="assistant", content=text)
            )
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
                tool_output=result
                if isinstance(result, str)
                else json.dumps(result)
                if result
                else None,
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
        message = None
        if isinstance(raw_error, str):
            message = raw_error.strip()
        elif isinstance(raw_error, dict):
            message = raw_error.get("message")
            if not isinstance(message, str):
                message = None
        if message:
            events.append(LiveEvent(kind="error", engine="gemini", error=message))
    return events


def _gemini_usage_window(
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
        if isinstance(total_token_count, int):
            return EngineUsageWindow(used=total_token_count, unit="tokens")
        return None

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
    if token_parts:
        return EngineUsageWindow(used=sum(token_parts), unit="tokens")
    return None


def _gemini_error_details(
    payload: dict[str, object],
) -> tuple[str | None, dict[str, str | int | bool | None], EngineUsageWindow | None]:
    event_type = str(payload.get("type", "")).lower()
    raw_error: object | None = None
    if event_type == "error":
        raw_error = (
            payload.get("value")
            or payload.get("data")
            or payload.get("error")
            or payload.get("message")
        )
    elif event_type == "result" and payload.get("status") == "error":
        raw_error = payload.get("error") or payload.get("data") or payload.get("message")
    if raw_error is None:
        return None, {}, None

    metadata: dict[str, str | int | bool | None] = {}
    message = _gemini_error_message(raw_error)
    usage = _gemini_error_usage(raw_error, metadata)
    _gemini_error_metadata(raw_error, metadata)
    if message:
        metadata["error_message"] = message
    return message, metadata, usage


def _gemini_error_usage(
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
                quota_metric = raw_violation.get("quotaMetric")
                quota_id = raw_violation.get("quotaId")
                normalized_limit_hint = " ".join(
                    value.lower()
                    for value in (quota_metric, quota_id)
                    if isinstance(value, str) and value
                )
                if "token" in normalized_limit_hint:
                    unit = "tokens"
                elif any(marker in normalized_limit_hint for marker in ("request", "rpm", "day")):
                    unit = "requests"
                break
        if detail_type == "type.googleapis.com/google.rpc.ErrorInfo":
            info_metadata = raw_detail.get("metadata")
            if not isinstance(info_metadata, dict):
                continue
            quota_reset = info_metadata.get("quotaResetTimeStamp")
            if isinstance(quota_reset, str) and quota_reset:
                reset_at = quota_reset
            reason = raw_detail.get("reason")
            if isinstance(reason, str) and reason in {
                "QUOTA_EXHAUSTED",
                "INSUFFICIENT_G1_CREDITS_BALANCE",
            }:
                remaining = 0

    if limit is None and unit is None and reset_at is None and remaining is None:
        return None
    if unit is None:
        unit = "requests"
    metadata.setdefault("quota_limit", limit)
    metadata.setdefault("quota_reset_at", reset_at)
    return EngineUsageWindow(limit=limit, remaining=remaining, unit=unit, reset_at=reset_at)


def _gemini_error_metadata(
    raw_error: object,
    metadata: dict[str, str | int | bool | None],
) -> None:
    nested = _decode_json_object(raw_error)
    if isinstance(nested, dict):
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
                        retry_delay_ms = _duration_to_millis(retry_delay)
                        if retry_delay_ms is not None:
                            metadata["retry_delay_ms"] = retry_delay_ms
                elif detail_type == "type.googleapis.com/google.rpc.ErrorInfo":
                    domain = raw_detail.get("domain")
                    if isinstance(domain, str) and domain:
                        metadata["error_domain"] = domain
                    reason = raw_detail.get("reason")
                    if isinstance(reason, str) and reason:
                        metadata["error_reason"] = reason
                    detail_metadata = raw_detail.get("metadata")
                    if isinstance(detail_metadata, dict):
                        quota_limit_name = detail_metadata.get("quota_limit")
                        if isinstance(quota_limit_name, str) and quota_limit_name:
                            metadata["quota_limit_name"] = quota_limit_name
                        quota_reset_at = detail_metadata.get("quotaResetTimeStamp")
                        if isinstance(quota_reset_at, str) and quota_reset_at:
                            metadata["quota_reset_at"] = quota_reset_at
                        quota_reset_delay = detail_metadata.get("quotaResetDelay")
                        if isinstance(quota_reset_delay, str) and quota_reset_delay:
                            metadata["quota_reset_delay"] = quota_reset_delay
                elif detail_type == "type.googleapis.com/google.rpc.QuotaFailure":
                    violations = raw_detail.get("violations")
                    if not isinstance(violations, list):
                        continue
                    for raw_violation in violations:
                        if not isinstance(raw_violation, dict):
                            continue
                        quota_metric = raw_violation.get("quotaMetric")
                        if isinstance(quota_metric, str) and quota_metric:
                            metadata["quota_metric"] = quota_metric
                        quota_id = raw_violation.get("quotaId")
                        if isinstance(quota_id, str) and quota_id:
                            metadata["quota_id"] = quota_id
                        quota_dimensions = raw_violation.get("quotaDimensions")
                        if isinstance(quota_dimensions, dict):
                            model = quota_dimensions.get("model")
                            if isinstance(model, str) and model:
                                metadata["quota_model"] = model
                        break
        nested_error = nested.get("error")
        if nested_error is not None:
            _gemini_error_metadata(nested_error, metadata)


def _gemini_error_message(raw_error: object) -> str | None:
    if isinstance(raw_error, str):
        nested = _decode_json_object(raw_error)
        if isinstance(nested, dict):
            return _gemini_error_message(nested)
        message = raw_error.strip()
        return message or None
    if isinstance(raw_error, dict):
        nested_error = raw_error.get("error")
        if isinstance(nested_error, dict):
            nested_message = _gemini_error_message(nested_error)
            if nested_message:
                return nested_message
        message = raw_error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def _duration_to_millis(value: str) -> int | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s)", value.strip())
    if match is None:
        return None
    magnitude = float(match.group(1))
    unit = match.group(2)
    if unit == "ms":
        return int(magnitude)
    return int(magnitude * 1000)
