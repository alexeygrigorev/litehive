"""Public Claude adapter module.

The ``ClaudeCLIAdapter`` class is part of heru's stable public adapter
contract. The imported ``_claude_impl`` helpers are internal.
"""

from pathlib import Path

from heru.adapters._claude_impl import (
    claude_continuation,
    claude_error_details,
    claude_stream_event_adapter,
    claude_usage_window,
    extract_claude_text_delta_fallback,
)
from heru.base import (
    CLIInvocation,
    ExternalCLIAdapter,
    build_invocation_env,
)

_extract_claude_text_delta_fallback = extract_claude_text_delta_fallback


class ClaudeCLIAdapter(ExternalCLIAdapter):
    """Public stable adapter for invoking Claude Code and parsing its stream."""

    DEFAULT_MODEL = "claude-sonnet-4-20250514"
    DEFAULT_NAME = "claude"
    DEFAULT_BINARY = "claude"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=True,
        strips_environment=False,
        transcript_format="jsonl",
    )
    SUPPORTS_CONTINUE_LATEST = True
    USAGE_PROVIDER = "anthropic"
    _MAX_ARG_PROMPT_BYTES: int = 120_000

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        prompt_via_stdin: bool = False,
    ) -> list[str]:
        command = [self.binary]
        if resume_session_id:
            if self.is_latest_continuation(resume_session_id):
                command.append("--continue")
            else:
                command.extend(["--resume", resume_session_id])
        if not prompt_via_stdin:
            command.extend(["-p", prompt])
        command.extend(
            ["--output-format", "stream-json", "--include-partial-messages", "--verbose", "--dangerously-skip-permissions"]
        )
        if model:
            command.extend(["--model", model])
        if max_turns is not None:
            command.extend(["--max-turns", str(max_turns)])
        return command

    def build_invocation(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        use_stdin = len(prompt.encode("utf-8")) > self._MAX_ARG_PROMPT_BYTES
        return CLIInvocation(
            argv=tuple(
                self.build_command(
                    prompt,
                    cwd,
                    model=model,
                    max_turns=max_turns,
                    resume_session_id=resume_session_id,
                    prompt_via_stdin=use_stdin,
                )
            ),
            cwd=cwd,
            env=build_invocation_env(
                cwd=cwd,
                stripped_env_vars=self.stripped_env_vars,
                extra_env=extra_env,
            ),
            stdin_data=prompt if use_stdin else None,
        )

    def transcript_assistant_text(self, execution) -> str:
        return self.extract_stream_transcript_text(
            execution.stdout,
            delta_fallback=extract_claude_text_delta_fallback,
        )

    def usage_window_from_payload(self, payload, metadata):
        return claude_usage_window(payload, metadata)

    def error_details_from_payload(self, payload):
        error_message, error_metadata = claude_error_details(payload)
        return error_message, error_metadata, None

    def scan_usage_payload(self, payload, state) -> None:
        error_message, error_metadata, _ = self.error_details_from_payload(payload)
        if payload.get("type") != "result":
            if error_message is None and not error_metadata:
                return
            if error_metadata:
                state.metadata.update(error_metadata)
            if error_message:
                state.metadata.setdefault("error_message", error_message)
                state.limit_reason = self.classify_limit_text(error_message, state.metadata)
            state.done = True
            return

        metadata: dict[str, str | int | bool | None] = dict(error_metadata)
        usage = self.usage_window_from_payload(payload, metadata)
        if usage is None and error_message is None and not metadata:
            return
        state.metadata = metadata
        state.usage = usage
        if error_message:
            state.metadata.setdefault("error_message", error_message)
            state.limit_reason = self.classify_limit_text(error_message, state.metadata)
        state.done = True

    def stream_event_adapter(self):
        return claude_stream_event_adapter()

    def continuation_from_payload(self, payload):
        return claude_continuation(payload)
