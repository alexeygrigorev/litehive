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
from heru.adapters.common import classify_execution_limit
from heru.base import (
    AdapterCapabilities,
    CLIExecutionResult,
    ExternalCLIAdapter,
    build_invocation_env,
    extract_jsonl_errors,
    extract_stream_errors,
    extract_stream_transcript,
    iter_jsonl_payloads,
)
from heru.types import RuntimeEngineContinuation, UnifiedEvent

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
    _MAX_ARG_PROMPT_BYTES: int = 120_000

    def supports_continue_latest(self) -> bool:
        return True

    def __init__(
        self,
        *,
        name: str | None = None,
        binary: str | None = None,
        capabilities: AdapterCapabilities | None = None,
        stripped_env_vars: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(
            name=name, binary=binary, capabilities=capabilities, stripped_env_vars=stripped_env_vars
        )

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
            command.append("--resume")
            if not self.is_latest_continuation(resume_session_id):
                command.append(resume_session_id)
            if not prompt_via_stdin:
                command.extend(["-p", prompt])
        elif not prompt_via_stdin:
            command.extend(["-p", prompt])
        command.extend(
            ["--output-format", "stream-json", "--include-partial-messages", "--verbose", "--dangerously-skip-permissions"]
        )
        if model:
            command.extend(["--model", model])
        if max_turns is not None:
            command.extend(["--max-turns", str(max_turns)])
        return command

    def build_invocation(self, prompt: str, cwd: Path, model: str | None = None, *, max_turns: int | None = None, resume_session_id: str | None = None, extra_env: dict[str, str] | None = None):
        from heru.base import CLIInvocation

        use_stdin = len(prompt.encode("utf-8")) > self._MAX_ARG_PROMPT_BYTES
        return CLIInvocation(
            argv=tuple(self.build_command(prompt, cwd, model=model, max_turns=max_turns, resume_session_id=resume_session_id, prompt_via_stdin=use_stdin)),
            cwd=cwd,
            env=build_invocation_env(
                cwd=cwd,
                stripped_env_vars=self.stripped_env_vars,
                extra_env=extra_env,
            ),
            stdin_data=prompt if use_stdin else None,
        )

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = extract_stream_transcript(
            execution.stdout,
            adapter=self.stream_event_adapter(),
            delta_fallback=extract_claude_text_delta_fallback,
        )
        return self.render_transcript_from_parts(execution, assistant_text=assistant_text)

    def parse_stage_report(self, *, task_id: str, step: str, execution: CLIExecutionResult, subagent_status: str):
        transcript = self.render_transcript(execution)
        fallback_errors = extract_stream_errors(execution.stdout, adapter=self.stream_event_adapter()) or extract_jsonl_errors(execution.stdout)
        return self.parse_stage_report_with_error_fallback(
            task_id=task_id,
            step=step,
            execution=execution,
            subagent_status=subagent_status,
            transcript=transcript,
            fallback_errors=fallback_errors,
        )

    def extract_usage_observation(self, execution: CLIExecutionResult):
        payloads = iter_jsonl_payloads(execution.stdout)
        for payload in reversed(payloads):
            error_message, error_metadata = claude_error_details(payload)
            if payload.get("type") != "result":
                if error_message is None and not error_metadata:
                    continue
                return self.usage_observation_from_scan(
                    execution,
                    provider="anthropic",
                    usage=None,
                    limit_reason=classify_execution_limit(error_message) if error_message else None,
                    metadata=error_metadata,
                    saw_payloads=bool(payloads),
                )
            metadata: dict[str, str | int | bool | None] = dict(error_metadata)
            usage = claude_usage_window(payload, metadata)
            if usage is None and error_message is None and not metadata:
                continue
            return self.usage_observation_from_scan(
                execution,
                provider="anthropic",
                usage=usage,
                limit_reason=classify_execution_limit(error_message) if error_message else None,
                metadata=metadata,
                saw_payloads=True,
            )
        return None

    def stream_event_adapter(self):
        return claude_stream_event_adapter()

    def translate_native_event(
        self,
        native_payload: dict[str, object],
    ) -> UnifiedEvent | None:
        return super().translate_native_event(native_payload)

    def extract_continuation(
        self,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        return self.extract_continuation_from_payloads(execution, claude_continuation)
