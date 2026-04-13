"""Public Gemini adapter module.

The ``GeminiCLIAdapter`` class is part of heru's stable public adapter
contract. The imported ``_gemini_impl`` helpers are internal.
"""

from pathlib import Path

from heru.adapters._gemini_impl import (
    gemini_continuation,
    gemini_error_details,
    gemini_stream_event_adapter,
    gemini_usage_window,
)
from heru.adapters.common import classify_execution_limit
from heru.base import (
    CLIExecutionResult,
    ExternalCLIAdapter,
    extract_jsonl_errors,
    extract_jsonl_messages,
    iter_jsonl_payloads,
)
from heru.types import RuntimeEngineContinuation, UnifiedEvent


class GeminiCLIAdapter(ExternalCLIAdapter):
    """Public stable adapter for invoking Gemini CLI and parsing JSONL."""

    DEFAULT_NAME = "gemini"
    DEFAULT_BINARY = "gemini"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=True,
        strips_environment=False,
        transcript_format="jsonl",
    )

    def supports_continue_latest(self) -> bool:
        return True

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
        if resume_session_id:
            command.append("--resume")
            if not self.is_latest_continuation(resume_session_id):
                command.append(resume_session_id)
        if model:
            command.extend(["-m", model])
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self.render_transcript_from_parts(
            execution,
            assistant_text=extract_jsonl_messages(execution.stdout),
        )

    def parse_stage_report(self, *, task_id: str, step: str, execution: CLIExecutionResult, subagent_status: str):
        return self.parse_stage_report_with_error_fallback(
            task_id=task_id,
            step=step,
            execution=execution,
            subagent_status=subagent_status,
            transcript=self.render_transcript(execution),
            fallback_errors=extract_jsonl_errors(execution.stdout),
        )

    def extract_usage_observation(self, execution: CLIExecutionResult):
        payloads = iter_jsonl_payloads(execution.stdout)
        metadata: dict[str, str | int | bool | None] = {}
        usage = None
        limit_reason = None
        for payload in reversed(payloads):
            if usage is None:
                usage = gemini_usage_window(payload, metadata)
            if limit_reason is None:
                error_message, error_metadata, error_usage = gemini_error_details(payload)
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
        return self.usage_observation_from_scan(
            execution,
            provider="google",
            usage=usage,
            limit_reason=limit_reason,
            metadata=metadata,
            saw_payloads=bool(payloads),
            stderr_limit_extractor=lambda stderr, _: classify_execution_limit(stderr),
        )

    def stream_event_adapter(self):
        return gemini_stream_event_adapter()

    def translate_native_event(
        self,
        native_payload: dict[str, object],
    ) -> UnifiedEvent | None:
        return super().translate_native_event(native_payload)

    def extract_continuation(
        self,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        return self.extract_continuation_from_payloads(execution, gemini_continuation)
