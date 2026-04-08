"""Codex CLI engine adapter."""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from litehive.engines.adapters.common import (
    _decode_json_object,
    classify_execution_limit,
)
from litehive.engines.base import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
    StreamEventAdapter,
    iter_jsonl_payloads,
    parse_stage_report_text,
)
from litehive.models import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CodexUsageLimitResult:
    limit_reason: str
    retry_at: str | None = None
    purchase_more_credits: bool = False


_CODEX_USAGE_LIMIT_RE = re.compile(
    r"you['\u2019]ve hit your usage limit", re.IGNORECASE
)


def _classify_codex_usage_limit(text: str | None) -> _CodexUsageLimitResult | None:
    if not text or not _CODEX_USAGE_LIMIT_RE.search(text):
        return None
    retry_at = _codex_retry_at_hint(text)
    if retry_at:
        logger.info("Codex usage limit hit; engine available again at %s", retry_at)
    else:
        logger.info("Codex usage limit hit; no reset date available")
    return _CodexUsageLimitResult(
        limit_reason="usage limit reached",
        retry_at=retry_at,
        purchase_more_credits="purchase more credits" in text.lower(),
    )


class CodexCLIAdapter(ExternalCLIAdapter):
    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        command = [
            self.binary,
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(cwd),
            "--skip-git-repo-check",
        ]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = _extract_codex_transcript(execution.stdout)
        error_text = "\n".join(_extract_codex_errors(execution.stdout)).strip()
        if assistant_text or error_text:
            parts = [part for part in (assistant_text, error_text) if part]
            if execution.stderr.strip():
                parts.append(f"[stderr]\n{execution.stderr.strip()}")
            return "\n\n".join(parts)
        if iter_jsonl_payloads(execution.stdout):
            if execution.stderr.strip():
                return f"[stderr]\n{execution.stderr.strip()}"
            return ""
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
        if not transcript:
            from litehive.engines.base import extract_codex_errors

            error_lines = extract_codex_errors(execution.stdout)
            if error_lines:
                transcript = "\n".join(error_lines)
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
        saw_payloads = bool(payloads)
        metadata: dict[str, str | int | bool | None] = {}
        usage: EngineUsageWindow | None = None
        limit_reason: str | None = None

        for payload in reversed(payloads):
            if usage is None:
                usage = _codex_usage_window(payload, metadata)
            if limit_reason is None:
                error_message, error_metadata = _codex_error_details(payload)
                if error_metadata:
                    metadata.update(error_metadata)
                if error_message:
                    metadata.setdefault("error_message", error_message)
                    codex_limit = _classify_codex_usage_limit(error_message)
                    if codex_limit:
                        limit_reason = codex_limit.limit_reason
                        if codex_limit.retry_at:
                            metadata.setdefault("retry_at_hint", codex_limit.retry_at)
                        if codex_limit.purchase_more_credits:
                            metadata.setdefault("purchase_more_credits", True)
                    else:
                        limit_reason = classify_execution_limit(error_message)
        payload_had_signal = usage is not None or bool(metadata)
        if limit_reason is None and execution.stderr.strip():
            codex_limit = _classify_codex_usage_limit(execution.stderr)
            if codex_limit:
                limit_reason = codex_limit.limit_reason
                if codex_limit.retry_at:
                    metadata.setdefault("retry_at_hint", codex_limit.retry_at)
                if codex_limit.purchase_more_credits:
                    metadata.setdefault("purchase_more_credits", True)
            else:
                limit_reason = classify_execution_limit(execution.stderr)
                retry_at_hint = _codex_retry_at_hint(execution.stderr)
                if retry_at_hint:
                    metadata["retry_at_hint"] = retry_at_hint
                if "purchase more credits" in execution.stderr.lower():
                    metadata["purchase_more_credits"] = True
        if not saw_payloads and not payload_had_signal:
            return None
        if usage is None and limit_reason is None and not metadata:
            return None
        return EngineUsageObservation(
            source="provider",
            provider="openai",
            success=execution.exit_code == 0,
            limit_reason=limit_reason,
            usage=usage,
            metadata=metadata,
        )


def _codex_live_events(payload: dict[str, object]) -> list[LiveEvent]:
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
                tool_name = None
                if isinstance(command, list) and command:
                    tool_name = str(command[0]) if command[0] else None
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


def _extract_codex_transcript(stdout: str) -> str:
    messages: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            logger.warning("codex transcript: item.completed has non-dict item field: %s (%.200s)", type(item).__name__, payload)
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            messages.append(text)
        else:
            logger.warning("codex transcript: agent_message missing text field (%.200s)", payload)
    return "\n".join(messages).strip()


def _extract_codex_errors(stdout: str) -> list[str]:
    errors: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        error_message, _ = _codex_error_details(payload)
        if error_message:
            errors.append(error_message)
    return errors


def _codex_usage_window(
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

    used_tokens: int | None = None
    if isinstance(total_tokens, int):
        used_tokens = total_tokens
    else:
        token_parts = [value for value in (input_tokens, output_tokens) if isinstance(value, int)]
        if token_parts:
            used_tokens = sum(token_parts)

    return EngineUsageWindow(used=used_tokens, unit="tokens") if used_tokens is not None else None


def _codex_error_details(
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
    message = _codex_error_message(raw_error)
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
    retry_at_hint = _codex_retry_at_hint(message)
    if retry_at_hint:
        metadata["retry_at_hint"] = retry_at_hint
    if "purchase more credits" in message.lower():
        metadata["purchase_more_credits"] = True
    return message, metadata


def _codex_error_message(raw_error: object) -> str | None:
    if isinstance(raw_error, str):
        nested = _decode_json_object(raw_error)
        if isinstance(nested, dict):
            return _codex_error_message(nested)
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


def _codex_retry_at_hint(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"try again at\s+([^.;]+)", text, re.IGNORECASE)
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None
