"""Public Goz adapter module.

The ``GozCLIAdapter`` class is part of heru's stable public adapter
contract. The imported ``_goz_impl`` helpers are internal.
"""

from pathlib import Path

from heru.adapters._goz_impl import (
    extract_goz_errors,
    extract_goz_transcript,
    goz_continuation,
    goz_extract_text,
    goz_error_details,
    goz_stream_event_adapter,
    goz_usage_window,
)
from heru.adapters.common import classify_execution_limit
from heru.base import CLIExecutionResult, ExternalCLIAdapter, iter_jsonl_payloads
from heru.types import RuntimeEngineContinuation, UnifiedEvent

_goz_extract_text = goz_extract_text


class GozCLIAdapter(ExternalCLIAdapter):
    """Public stable adapter for invoking Goz and normalizing its output."""

    DEFAULT_NAME = "goz"
    DEFAULT_BINARY = "goz"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=True,
        strips_environment=False,
        transcript_format="jsonl",
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
        command = [self.binary, "run", "--format", "json"]
        if resume_session_id:
            if self.is_latest_continuation(resume_session_id):
                raise ValueError("goz does not support resuming the latest session without an explicit id")
            command.extend(["--resume-session", resume_session_id])
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self.render_transcript_from_parts(
            execution,
            assistant_text=extract_goz_transcript(execution.stdout),
            error_text="\n".join(extract_goz_errors(execution.stdout)).strip(),
        )

    def parse_stage_report(self, *, task_id: str, step: str, execution: CLIExecutionResult, subagent_status: str):
        return self.parse_stage_report_with_error_fallback(
            task_id=task_id,
            step=step,
            execution=execution,
            subagent_status=subagent_status,
            transcript=self.render_transcript(execution),
            fallback_errors=extract_goz_errors(execution.stdout),
        )

    def extract_usage_observation(self, execution: CLIExecutionResult):
        payloads = iter_jsonl_payloads(execution.stdout)
        metadata: dict[str, str | int | bool | None] = {}
        usage = None
        limit_reason = None
        for payload in reversed(payloads):
            if usage is None:
                usage = goz_usage_window(payload, metadata)
            if limit_reason is None:
                error_message, error_metadata = goz_error_details(payload)
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
        return goz_stream_event_adapter()

    def translate_native_event(
        self,
        native_payload: dict[str, object],
    ) -> UnifiedEvent | None:
        return super().translate_native_event(native_payload)

    def extract_continuation(
        self,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        return self.extract_continuation_from_payloads(execution, goz_continuation)
