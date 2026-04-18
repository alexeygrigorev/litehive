"""Public Copilot adapter module.

The ``CopilotCLIAdapter`` class is part of heru's stable public adapter
contract. The imported ``_copilot_impl`` helpers are internal.
"""

from pathlib import Path

from heru.adapters._copilot_impl import (
    copilot_continuation,
    copilot_stream_event_adapter,
    copilot_usage_observation,
)
from heru.base import ExternalCLIAdapter


class CopilotCLIAdapter(ExternalCLIAdapter):
    """Public stable adapter for invoking GitHub Copilot CLI."""

    DEFAULT_NAME = "copilot"
    DEFAULT_BINARY = "copilot"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=True,
        strips_environment=False,
        transcript_format="jsonl",
    )
    SUPPORTS_CONTINUE_LATEST = True
    USAGE_PROVIDER = "github"

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
        if resume_session_id:
            if self.is_latest_continuation(resume_session_id):
                command.append("--continue")
            else:
                command.append(f"--resume={resume_session_id}")
        if model:
            command.extend(["--model", model])
        return command

    def transcript_assistant_text(self, execution) -> str:
        return self.extract_stream_transcript_text(execution.stdout)

    def scan_usage_payload(self, payload, state) -> None:
        observed = copilot_usage_observation(payload)
        if observed is None:
            return
        usage, metadata = observed
        state.usage = usage
        state.metadata = metadata
        state.done = True

    def stream_event_adapter(self):
        return copilot_stream_event_adapter()

    def continuation_from_payload(self, payload):
        return copilot_continuation(payload)
