"""External CLI engine adapters."""

from __future__ import annotations

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

class EngineError(RuntimeError):
    """Raised when an engine cannot be resolved or executed."""


_ENGINE_LIMIT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("quota exceeded", "quota exceeded"),
    ("quota", "quota limit reached"),
    ("rate limit", "rate limit reached"),
    ("too many requests", "rate limit reached"),
    ("budget", "budget limit reached"),
    ("credit", "credit limit reached"),
    ("insufficient funds", "budget limit reached"),
    ("usage limit", "usage limit reached"),
    ("capacity", "capacity limit reached"),
)


class CodexCLIAdapter(ExternalCLIAdapter):
    def build_command(self, prompt: str, cwd: Path, model: str | None = None) -> list[str]:
        return [
            self.binary,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(cwd),
            "--skip-git-repo-check",
            prompt,
        ]


class OpenCodeAdapter(ExternalCLIAdapter):
    def build_command(self, prompt: str, cwd: Path, model: str | None = None) -> list[str]:
        command = [self.binary, "run", "--dir", str(cwd)]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command


class GeminiCLIAdapter(ExternalCLIAdapter):
    def build_command(self, prompt: str, cwd: Path, model: str | None = None) -> list[str]:
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


class CopilotCLIAdapter(ExternalCLIAdapter):
    def build_command(self, prompt: str, cwd: Path, model: str | None = None) -> list[str]:
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


ENGINE_REGISTRY: dict[str, ExternalCLIAdapter] = {
    "codex": CodexCLIAdapter(
        name="codex",
        binary="codex",
        capabilities=AdapterCapabilities(
            supports_model_override=False,
            strips_environment=False,
            transcript_format="text",
        ),
    ),
    "opencode": OpenCodeAdapter(
        name="opencode",
        binary="opencode",
        capabilities=AdapterCapabilities(
            supports_model_override=True,
            strips_environment=True,
            transcript_format="text",
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
}


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
