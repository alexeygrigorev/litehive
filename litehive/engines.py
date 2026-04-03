"""External CLI engine adapters."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path

from litehive.external_cli import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
    extract_jsonl_errors,
    extract_jsonl_messages,
    iter_jsonl_payloads,
    parse_stage_report_text,
)
from litehive.models import EngineUsageObservation, EngineUsageWindow


class EngineError(RuntimeError):
    """Raised when an engine cannot be resolved or executed."""


_ENGINE_LIMIT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("hit your usage limit", "usage limit reached"),
    ("usage limit", "usage limit reached"),
    ("spend limit", "budget limit reached"),
    ("quota exceeded", "quota exceeded"),
    ("quota", "quota limit reached"),
    ("rate_limit_error", "rate limit reached"),
    ("rate limit", "rate limit reached"),
    ("too many requests", "rate limit reached"),
    ("budget", "budget limit reached"),
    ("credit", "credit limit reached"),
    ("insufficient funds", "budget limit reached"),
    ("purchase more credits", "usage limit reached"),
    ("capacity", "capacity limit reached"),
)

_RETRYABLE_EXECUTION_PATTERNS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "timeout",
        (
            "timed out",
            "timeout",
            "deadline exceeded",
            "etimedout",
            "operation timed out",
            "request timed out",
            "upstream request timeout",
            "read timeout",
            "connect timeout",
            "request_timeout",
            "request timeout",
        ),
        "transient timeout",
    ),
    (
        "network",
        (
            "connection reset",
            "connection refused",
            "network error",
            "temporary failure in name resolution",
            "network is unreachable",
            "socket hang up",
            "econnreset",
            "econnrefused",
            "eai_again",
            "enotfound",
            "broken pipe",
            "connection closed",
            "connection aborted",
            "connection interrupted",
            "error sending request",
            "error trying to connect",
            "peer closed connection",
            "tls handshake eof",
            "socket disconnected before secure tls connection was established",
            "unexpected eof",
            "client network socket disconnected",
            "connect econnrefused",
            "connect enetunreach",
            "connect ehostunreach",
            "write epipe",
            "getaddrinfo eai_again",
            "getaddrinfo enotfound",
            "network connection was lost",
            "connection has been closed",
        ),
        "transient network failure",
    ),
    (
        "service",
        (
            "internal server error",
            "bad gateway",
            "service unavailable",
            "service temporarily unavailable",
            "temporarily unavailable",
            "gateway timeout",
            "server overloaded",
            "overloaded",
            "try again later",
            "server error",
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
            "status code 502",
            "status code 503",
            "status code 504",
            "status: 500",
            "status: 502",
            "status: 503",
            "status: 504",
            "backend error",
            "backend unavailable",
            "api_error",
            "overloaded_error",
            "529",
            "anthropic's systems are overloaded",
        ),
        "transient service failure",
    ),
)

_EXECUTION_INTERRUPTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("keyboardinterrupt", "execution interrupted"),
    ("interrupt signal", "execution interrupted"),
    ("received sigint", "execution interrupted"),
    ("received signal sigint", "execution interrupted"),
    ("received signal 2", "execution interrupted"),
    ("terminated by sigint", "execution interrupted"),
    ("terminated by signal 2", "execution interrupted"),
    ("cancelled by user", "execution interrupted"),
    ("canceled by user", "execution interrupted"),
    ("interrupted by user", "execution interrupted"),
    ("execution interrupted", "execution interrupted"),
)


@dataclass(frozen=True, slots=True)
class RetryableExecutionFailure:
    classification: str
    reason: str


class CodexCLIAdapter(ExternalCLIAdapter):
    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
    ) -> list[str]:
        return [
            self.binary,
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(cwd),
            "--skip-git-repo-check",
            prompt,
        ]

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = _extract_codex_transcript(execution.stdout)
        error_text = "\n".join(_extract_codex_errors(execution.stdout)).strip()
        if assistant_text or error_text:
            parts = [part for part in (assistant_text, error_text) if part]
            if execution.stderr.strip():
                parts.append(f"[stderr]\n{execution.stderr.strip()}")
            return "\n\n".join(parts)
        return execution.transcript

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
                    limit_reason = classify_execution_limit(error_message)
        payload_had_signal = usage is not None or bool(metadata)
        if limit_reason is None and execution.stderr.strip():
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


class OpenCodeAdapter(ExternalCLIAdapter):
    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
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


class GeminiCLIAdapter(ExternalCLIAdapter):
    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
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


class ClaudeCLIAdapter(ExternalCLIAdapter):
    DEFAULT_MODEL = "claude-sonnet-4-20250514"

    def __init__(
        self,
        *,
        name: str,
        binary: str,
        capabilities: AdapterCapabilities,
        stripped_env_vars: tuple[str, ...] = (),
        max_turns: int = 30,
    ) -> None:
        super().__init__(
            name=name, binary=binary, capabilities=capabilities, stripped_env_vars=stripped_env_vars
        )
        self.max_turns = max_turns

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
    ) -> list[str]:
        command = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if model:
            command.extend(["--model", model])
        effective_max_turns = self.max_turns if max_turns is None else max_turns
        command.extend(["--max-turns", str(effective_max_turns)])
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = _extract_claude_transcript(execution.stdout)
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


class CopilotCLIAdapter(ExternalCLIAdapter):
    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
    ) -> list[str]:
        command = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--allow-all-tools",
            "--autopilot",
            "--no-auto-update",
            "--add-dir",
            str(cwd),
        ]
        if model:
            command.extend(["--model", model])
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = _extract_copilot_transcript(execution.stdout)
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
            error_lines = _extract_copilot_errors(execution.stdout)
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
        for payload in reversed(payloads):
            if payload.get("type") != "assistant.usage":
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                continue
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
                selected_name, selected_snapshot = _select_copilot_quota_snapshot(quota_snapshots)
                if isinstance(selected_snapshot, dict):
                    quota_usage = _copilot_quota_usage_window(selected_snapshot)
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

            return EngineUsageObservation(
                source="provider",
                provider="github",
                success=execution.exit_code == 0,
                usage=usage,
                metadata=metadata,
            )
        return None


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


def _extract_claude_transcript(stdout: str) -> str:
    messages: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        event_type = payload.get("type")
        if event_type == "assistant":
            data = payload.get("message")
            if isinstance(data, dict):
                content = data.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                messages.append(text)
                elif isinstance(content, str) and content:
                    messages.append(content)
        elif event_type == "result":
            data = payload.get("result")
            if isinstance(data, str) and data:
                messages.append(data)
    return "\n".join(messages).strip()


def _extract_codex_transcript(stdout: str) -> str:
    messages: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            messages.append(text)
    return "\n".join(messages).strip()


def _extract_codex_errors(stdout: str) -> list[str]:
    errors: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        error_message, _ = _codex_error_details(payload)
        if error_message:
            errors.append(error_message)
    return errors


def _extract_opencode_transcript(stdout: str) -> str:
    messages: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") != "text":
            continue
        part = payload.get("part")
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            messages.append(text)
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


def _decode_json_object(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, str):
        return raw if isinstance(raw, dict) else None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _codex_retry_at_hint(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"try again at\s+([^.;]+)", text, re.IGNORECASE)
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


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


ENGINE_REGISTRY: dict[str, ExternalCLIAdapter] = {
    "codex": CodexCLIAdapter(
        name="codex",
        binary="codex",
        capabilities=AdapterCapabilities(
            supports_model_override=False,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
    "opencode": OpenCodeAdapter(
        name="opencode",
        binary="opencode",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=True,
            transcript_format="jsonl",
        ),
        stripped_env_vars=_OPENCODE_STRIPPED_ENV_VARS,
    ),
    "gemini": GeminiCLIAdapter(
        name="gemini",
        binary="gemini",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
    "copilot": CopilotCLIAdapter(
        name="copilot",
        binary="copilot",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
    "claude": ClaudeCLIAdapter(
        name="claude",
        binary="claude",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=False,
            transcript_format="jsonl",
        ),
    ),
}

ENGINE_CHOICES = sorted(ENGINE_REGISTRY.keys())


def get_engine(name: str) -> ExternalCLIAdapter:
    try:
        return ENGINE_REGISTRY[name]
    except KeyError as exc:
        raise EngineError(f"Unknown engine '{name}'") from exc


def classify_execution_limit(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    for needle, reason in _ENGINE_LIMIT_PATTERNS:
        if needle in normalized:
            return reason
    return None


def classify_execution_interruption(text: str, *, exit_code: int | None = None) -> str | None:
    if exit_code in {130, 131, 143}:
        return "execution interrupted"
    if exit_code is not None and exit_code < 0 and abs(exit_code) in {2, 15}:
        return "execution interrupted"
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if not normalized:
        return None
    for needle, reason in _EXECUTION_INTERRUPTION_PATTERNS:
        if needle in normalized:
            return reason
    return None


def classify_retryable_execution_failure(text: str) -> RetryableExecutionFailure | None:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if (
        not normalized
        or classify_execution_limit(normalized) is not None
        or classify_execution_interruption(normalized) is not None
    ):
        return None
    for classification, needles, reason in _RETRYABLE_EXECUTION_PATTERNS:
        if any(needle in normalized for needle in needles):
            return RetryableExecutionFailure(classification=classification, reason=reason)
    return None


def _extract_copilot_transcript(stdout: str) -> str:
    final_messages: list[str] = []
    deltas: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        event_type = payload.get("type")
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content:
                final_messages.append(content)
        elif event_type == "assistant.message_delta":
            content = data.get("deltaContent")
            if isinstance(content, str) and content:
                deltas.append(content)
    if final_messages:
        return "".join(final_messages).strip()
    return "".join(deltas).strip()


def _extract_copilot_errors(stdout: str) -> list[str]:
    errors = extract_jsonl_errors(stdout)
    if errors:
        return errors

    tool_errors: list[str] = []
    for payload in iter_jsonl_payloads(stdout):
        if payload.get("type") != "tool.execution_complete":
            continue
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("success", True):
            continue
        result = data.get("result")
        if isinstance(result, dict):
            content = result.get("content") or result.get("detailedContent")
            if isinstance(content, str) and content:
                tool_errors.append(content)
        elif isinstance(result, str) and result:
            tool_errors.append(result)
    return tool_errors


def _copilot_quota_usage_window(snapshot: dict[str, object]) -> EngineUsageWindow | None:
    entitlement_requests = snapshot.get("entitlementRequests")
    used_requests = snapshot.get("usedRequests")
    if not isinstance(used_requests, int):
        return None
    remaining: int | None = None
    if isinstance(entitlement_requests, int):
        remaining = max(entitlement_requests - used_requests, 0)
    reset_date = snapshot.get("resetDate")
    return EngineUsageWindow(
        used=used_requests,
        limit=entitlement_requests if isinstance(entitlement_requests, int) else None,
        remaining=remaining,
        unit="requests",
        reset_at=reset_date if isinstance(reset_date, str) and reset_date else None,
    )


def _select_copilot_quota_snapshot(
    snapshots: dict[object, object],
) -> tuple[str, dict[str, object] | None]:
    preferred_names = ("premium_interactions", "chat", "completions")
    normalized: dict[str, dict[str, object]] = {
        str(name): value for name, value in snapshots.items() if isinstance(value, dict)
    }
    for name in preferred_names:
        selected = normalized.get(name)
        if selected is not None:
            return name, selected
    for name, snapshot in normalized.items():
        return name, snapshot
    return "", None
