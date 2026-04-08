"""OpenCode CLI engine adapter."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from litehive.engines.adapters.common import classify_execution_limit
from litehive.engines.base import (
    CLIExecutionResult,
    ExternalCLIAdapter,
    iter_jsonl_payloads,
    parse_stage_report_text,
)
from litehive.models import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
)


_OPENCODE_STRIPPED_ENV_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "GROQ_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_API_KEY",
    "VERTEX_LOCATION",
    "VERTEX_AI_PROJECT",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "FIREWORKS_API_KEY",
    "CEREBRAS_API_KEY",
    "OPENROUTER_API_KEY",
    "TOGETHER_API_KEY",
    "TOGETHER_AI_API_KEY",
    "AZURE_API_KEY",
    "AZURE_RESOURCE_NAME",
    "AZURE_COGNITIVE_SERVICES_RESOURCE_NAME",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_GATEWAY_ID",
    "CLOUDFLARE_API_KEY",
    "HUGGING_FACE_API_KEY",
    "HF_TOKEN",
    "HF_API_TOKEN",
    "MOONSHOT_API_KEY",
    "MOONSHOTAI_API_KEY",
    "MINIMAX_API_KEY",
    "NEBIUS_API_KEY",
    "DEEPINFRA_API_KEY",
    "BASETEN_API_KEY",
    "VENICE_API_KEY",
    "SCALEWAY_API_KEY",
    "OVH_API_KEY",
    "CORTECS_API_KEY",
    "IONET_API_KEY",
    "VERCEL_API_KEY",
    "ZENMUX_API_KEY",
    "ZAI_API_KEY",
    "HELICONE_API_KEY",
    "OPENCODE_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "GITLAB_TOKEN",
    "GITLAB_INSTANCE_URL",
    "GITLAB_AI_GATEWAY_URL",
    "GITLAB_OAUTH_CLIENT_ID",
    "AICORE_SERVICE_KEY",
    "AICORE_DEPLOYMENT_ID",
    "AICORE_RESOURCE_GROUP",
    "OPENAI_COMPATIBLE_API_KEY",
    "LMSTUDIO_API_KEY",
    "OLLAMA_API_KEY",
    "302AI_API_KEY",
    "FIRMWARE_API_KEY",
    "2AI_API_KEY",
    "GEMINI_API_KEY",
)


class OpenCodeAdapter(ExternalCLIAdapter):
    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        command = [self.binary, "run", "--format", "json", "--dir", str(cwd)]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = _extract_opencode_transcript(execution.stdout)
        error_text = _extract_opencode_errors(execution.stdout).strip()
        if assistant_text or error_text:
            parts = [part for part in (assistant_text, error_text) if part]
            if execution.stderr.strip():
                parts.append(f"[stderr]\n{execution.stderr.strip()}")
            return "\n\n".join(parts)
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
            error_text = _extract_opencode_errors(execution.stdout).strip()
            if error_text:
                transcript = error_text
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
        saw_payloads = bool(payloads)

        for payload in reversed(payloads):
            if usage is None:
                usage = _opencode_usage_window(payload, metadata)
            if limit_reason is None:
                error_message, error_metadata = _opencode_error_details(payload)
                if error_metadata:
                    metadata.update(error_metadata)
                if error_message:
                    metadata.setdefault("error_message", error_message)
                    limit_reason = classify_execution_limit(error_message)

        if limit_reason is None and execution.stderr.strip():
            limit_reason = classify_execution_limit(execution.stderr)
        if not saw_payloads:
            return None
        if usage is None and limit_reason is None and not metadata:
            return None
        return EngineUsageObservation(
            source="provider",
            provider="z.ai",
            success=execution.exit_code == 0,
            limit_reason=limit_reason,
            usage=usage,
            metadata=metadata,
        )


def _opencode_live_events(payload: dict[str, object]) -> list[LiveEvent]:
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
                events.append(LiveEvent(kind="error", engine="opencode", error=message.strip()))
    return events


def _extract_opencode_transcript(stdout: str) -> str:
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
            logger.warning(
                "opencode: text event part has no text field (keys=%s)",
                list(part.keys()),
            )
    return "\n".join(part.rstrip() for part in messages if part.strip()).strip()


def _extract_opencode_errors(stdout: str) -> str:
    errors: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        error_message, _ = _opencode_error_details(payload)
        if error_message:
            errors.append(error_message)
    return "\n".join(errors).strip()


def _opencode_usage_window(
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

    used_tokens: int | None = None
    if isinstance(total_tokens, int):
        used_tokens = total_tokens
    else:
        token_parts = [value for value in (input_tokens, output_tokens) if isinstance(value, int)]
        if token_parts:
            used_tokens = sum(token_parts)
    return EngineUsageWindow(used=used_tokens, unit="tokens") if used_tokens is not None else None


def _opencode_error_details(
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
    if isinstance(name, str) and name.strip():
        return name.strip(), metadata
    return None, metadata
