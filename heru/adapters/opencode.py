"""Public OpenCode adapter module.

The ``OpenCodeAdapter`` class is part of heru's stable public adapter
contract under its current exported name. The imported
``_opencode_impl`` helpers are internal.
"""

from pathlib import Path

from heru.adapters._opencode_impl import (
    extract_opencode_errors,
    extract_opencode_transcript,
    opencode_continuation,
    opencode_error_details,
    opencode_stream_event_adapter,
    opencode_usage_window,
)
from heru.adapters.common import classify_execution_limit
from heru.base import CLIExecutionResult, ExternalCLIAdapter, iter_jsonl_payloads
from heru.types import RuntimeEngineContinuation, UnifiedEvent

_extract_opencode_transcript = extract_opencode_transcript


class OpenCodeAdapter(ExternalCLIAdapter):
    """Public stable adapter for invoking OpenCode and parsing its stream."""

    DEFAULT_NAME = "opencode"
    DEFAULT_BINARY = "opencode"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=True,
        strips_environment=True,
        transcript_format="jsonl",
    )
    def supports_continue_latest(self) -> bool:
        return True

    DEFAULT_STRIPPED_ENV_VARS = (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_REGION",
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "OPENAI_API_KEY",
        "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN", "GROQ_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_API_KEY", "VERTEX_LOCATION", "VERTEX_AI_PROJECT", "DEEPSEEK_API_KEY", "XAI_API_KEY",
        "FIREWORKS_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "TOGETHER_API_KEY", "TOGETHER_AI_API_KEY",
        "AZURE_API_KEY", "AZURE_RESOURCE_NAME", "AZURE_COGNITIVE_SERVICES_RESOURCE_NAME", "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_GATEWAY_ID",
        "CLOUDFLARE_API_KEY", "HUGGING_FACE_API_KEY", "HF_TOKEN", "HF_API_TOKEN", "MOONSHOT_API_KEY",
        "MOONSHOTAI_API_KEY", "MINIMAX_API_KEY", "NEBIUS_API_KEY", "DEEPINFRA_API_KEY", "BASETEN_API_KEY",
        "VENICE_API_KEY", "SCALEWAY_API_KEY", "OVH_API_KEY", "CORTECS_API_KEY", "IONET_API_KEY",
        "VERCEL_API_KEY", "ZENMUX_API_KEY", "ZAI_API_KEY", "HELICONE_API_KEY", "OPENCODE_API_KEY",
        "OPENCODE_ZEN_API_KEY", "GITLAB_TOKEN", "GITLAB_INSTANCE_URL", "GITLAB_AI_GATEWAY_URL",
        "GITLAB_OAUTH_CLIENT_ID", "AICORE_SERVICE_KEY", "AICORE_DEPLOYMENT_ID", "AICORE_RESOURCE_GROUP",
        "OPENAI_COMPATIBLE_API_KEY", "LMSTUDIO_API_KEY", "OLLAMA_API_KEY", "302AI_API_KEY", "FIRMWARE_API_KEY",
        "2AI_API_KEY", "GEMINI_API_KEY",
    )

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
        if resume_session_id:
            if self.is_latest_continuation(resume_session_id):
                command.append("--continue")
            else:
                command.extend(["--session", resume_session_id])
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self.render_transcript_from_parts(
            execution,
            assistant_text=extract_opencode_transcript(execution.stdout),
            error_text=extract_opencode_errors(execution.stdout).strip(),
        )

    def parse_stage_report(self, *, task_id: str, step: str, execution: CLIExecutionResult, subagent_status: str):
        return self.parse_stage_report_with_error_fallback(
            task_id=task_id,
            step=step,
            execution=execution,
            subagent_status=subagent_status,
            transcript=self.render_transcript(execution),
            fallback_errors=[extract_opencode_errors(execution.stdout).strip()] if extract_opencode_errors(execution.stdout).strip() else [],
        )

    def extract_usage_observation(self, execution: CLIExecutionResult):
        payloads = iter_jsonl_payloads(execution.stdout)
        metadata: dict[str, str | int | bool | None] = {}
        usage = None
        limit_reason = None
        for payload in reversed(payloads):
            if usage is None:
                usage = opencode_usage_window(payload, metadata)
            if limit_reason is None:
                error_message, error_metadata = opencode_error_details(payload)
                if error_metadata:
                    metadata.update(error_metadata)
                if error_message:
                    metadata.setdefault("error_message", error_message)
                    limit_reason = classify_execution_limit(error_message)
        return self.usage_observation_from_scan(
            execution,
            provider="z.ai",
            usage=usage,
            limit_reason=limit_reason,
            metadata=metadata,
            saw_payloads=bool(payloads),
            require_payloads=True,
            stderr_limit_extractor=lambda stderr, _: classify_execution_limit(stderr),
        )

    def stream_event_adapter(self):
        return opencode_stream_event_adapter()

    def translate_native_event(
        self,
        native_payload: dict[str, object],
    ) -> UnifiedEvent | None:
        return super().translate_native_event(native_payload)

    def extract_continuation(
        self,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        return self.extract_continuation_from_payloads(execution, opencode_continuation)
