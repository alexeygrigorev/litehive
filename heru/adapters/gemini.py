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
from heru.base import (
    ExternalCLIAdapter,
    extract_jsonl_messages,
)


class GeminiCLIAdapter(ExternalCLIAdapter):
    """Public stable adapter for invoking Gemini CLI and parsing JSONL."""

    DEFAULT_NAME = "gemini"
    DEFAULT_BINARY = "gemini"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=True,
        strips_environment=False,
        transcript_format="jsonl",
    )
    SUPPORTS_CONTINUE_LATEST = True
    USAGE_PROVIDER = "google"

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

    def transcript_assistant_text(self, execution) -> str:
        return extract_jsonl_messages(execution.stdout)

    def usage_window_from_payload(self, payload, metadata):
        return gemini_usage_window(payload, metadata)

    def error_details_from_payload(self, payload):
        return gemini_error_details(payload)

    def update_usage_metadata_from_payload(self, payload, metadata) -> None:
        if payload.get("type") != "init":
            return
        model = payload.get("model")
        if isinstance(model, str) and model:
            metadata.setdefault("model", model)

    def classify_stderr_limit(self, stderr, metadata):
        return self.classify_limit_text(stderr, metadata)

    def stream_event_adapter(self):
        return gemini_stream_event_adapter()

    def continuation_from_payload(self, payload):
        return gemini_continuation(payload)
