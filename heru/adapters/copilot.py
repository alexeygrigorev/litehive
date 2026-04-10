"""Copilot CLI engine adapter."""

from pathlib import Path

from heru.adapters._copilot_impl import (
    copilot_stream_event_adapter,
    copilot_usage_observation,
)
from heru.base import (
    CLIExecutionResult,
    ExternalCLIAdapter,
    extract_stream_errors,
    extract_stream_transcript,
    iter_jsonl_payloads,
)


class CopilotCLIAdapter(ExternalCLIAdapter):
    DEFAULT_NAME = "copilot"
    DEFAULT_BINARY = "copilot"
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
            command.append("--continue" if resume_session_id == "latest" else f"--resume={resume_session_id}")
        if model:
            command.extend(["--model", model])
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self.render_transcript_from_parts(
            execution,
            assistant_text=extract_stream_transcript(
                execution.stdout,
                adapter=self.stream_event_adapter(),
            ),
        )

    def parse_stage_report(self, *, task_id: str, step: str, execution: CLIExecutionResult, subagent_status: str):
        return self.parse_stage_report_with_error_fallback(
            task_id=task_id,
            step=step,
            execution=execution,
            subagent_status=subagent_status,
            transcript=self.render_transcript(execution),
            fallback_errors=extract_stream_errors(execution.stdout, adapter=self.stream_event_adapter()),
        )

    def extract_usage_observation(self, execution: CLIExecutionResult):
        payloads = iter_jsonl_payloads(execution.stdout)
        for payload in reversed(payloads):
            observed = copilot_usage_observation(payload)
            if observed is None:
                continue
            usage, metadata = observed
            return self.usage_observation_from_scan(
                execution,
                provider="github",
                usage=usage,
                limit_reason=None,
                metadata=metadata,
                saw_payloads=True,
            )
        return None

    def stream_event_adapter(self):
        return copilot_stream_event_adapter()
