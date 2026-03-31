"""External CLI engine adapters."""

from __future__ import annotations

from pathlib import Path

from litehive.external_cli import AdapterCapabilities, ExternalCLIAdapter

class EngineError(RuntimeError):
    """Raised when an engine cannot be resolved or executed."""


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
}


def get_engine(name: str) -> ExternalCLIAdapter:
    try:
        return ENGINE_REGISTRY[name]
    except KeyError as exc:
        raise EngineError(f"Unknown engine '{name}'") from exc
