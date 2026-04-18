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
from heru.base import ExternalCLIAdapter

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
    USAGE_PROVIDER = "z.ai"
    REQUIRE_USAGE_PAYLOADS = True

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

    def transcript_assistant_text(self, execution) -> str:
        return extract_goz_transcript(execution.stdout)

    def transcript_error_text(self, execution) -> str:
        return "\n".join(extract_goz_errors(execution.stdout)).strip()

    def usage_window_from_payload(self, payload, metadata):
        return goz_usage_window(payload, metadata)

    def error_details_from_payload(self, payload):
        error_message, error_metadata = goz_error_details(payload)
        return error_message, error_metadata, None

    def classify_stderr_limit(self, stderr, metadata):
        return self.classify_limit_text(stderr, metadata)

    def stream_event_adapter(self):
        return goz_stream_event_adapter()

    def continuation_from_payload(self, payload):
        return goz_continuation(payload)
