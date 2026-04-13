"""Public Codex adapter module.

The ``CodexCLIAdapter`` class defined here is part of heru's stable
public adapter contract. The imported ``_codex_impl`` helpers are
internal implementation details.
"""

import logging
from pathlib import Path

from heru.adapters.common import classify_execution_limit
from heru.adapters._codex_impl import (
    classify_codex_usage_limit as _classify_codex_usage_limit,
    codex_continuation,
    codex_error_details,
    extract_codex_errors,
    extract_codex_messages,
    iter_codex_payloads,
    codex_stderr_limit,
    codex_stream_event_adapter,
    codex_usage_window,
)
from heru.base import (
    CLIExecutionResult,
    ExternalCLIAdapter,
    parse_stage_report_text,
)
from heru.types import RuntimeEngineContinuation, UnifiedEvent


logger = logging.getLogger("litehive.agents.adapters.codex")


def _extract_codex_transcript(stdout: str) -> str:
    messages: dict[str, str] = {}
    for payload in iter_codex_payloads(stdout):
        if payload.get("type") not in {"item.completed", "item.updated"}:
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            logger.warning("codex: item.completed contains non-dict item (type=%s)", type(item).__name__)
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            messages[item_id] = text
        else:
            logger.warning("codex: agent_message has no text field (keys=%s)", list(item.keys()))
    return "\n".join(messages.values()).strip()


class CodexCLIAdapter(ExternalCLIAdapter):
    """Public stable adapter for invoking Codex and normalizing its output."""

    DEFAULT_NAME = "codex"
    DEFAULT_BINARY = "codex"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=False,
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
        # Two shapes:
        #   - fresh:  codex exec --json ... --cd <cwd> [--model M] <prompt>
        #   - resume: codex exec resume --json ... [--model M] <session_id> <prompt>
        # `codex exec resume` does not accept --cd — the subprocess cwd is
        # already set to the worktree path by the caller, so codex inherits it.
        command: list[str] = [self.binary, "exec"]
        latest_resume = self.is_latest_continuation(resume_session_id)
        if resume_session_id:
            command.append("resume")
        command.extend([
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ])
        if not resume_session_id:
            command.extend(["--cd", str(cwd)])
        if model:
            command.extend(["--model", model])
        if resume_session_id and not latest_resume:
            command.append(resume_session_id)
        command.append(prompt)
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        assistant_text = extract_codex_messages(execution.stdout)
        error_text = "\n".join(extract_codex_errors(execution.stdout)).strip()
        if assistant_text or error_text:
            parts = [part for part in (assistant_text, error_text) if part]
            if execution.stderr.strip():
                parts.append(f"[stderr]\n{execution.stderr.strip()}")
            return "\n\n".join(parts)
        if iter_codex_payloads(execution.stdout):
            return f"[stderr]\n{execution.stderr.strip()}" if execution.stderr.strip() else ""
        return execution.transcript

    def parse_stage_report(self, *, task_id: str, step: str, execution: CLIExecutionResult, subagent_status: str):
        transcript = self.render_transcript(execution)
        if not transcript:
            transcript = "\n".join(extract_codex_errors(execution.stdout))
        return parse_stage_report_text(task_id=task_id, step=step, transcript=transcript, subagent_status=subagent_status)

    def extract_usage_observation(self, execution: CLIExecutionResult):
        payloads = iter_codex_payloads(execution.stdout)
        metadata: dict[str, str | int | bool | None] = {}
        usage = None
        limit_reason = None
        for payload in reversed(payloads):
            if usage is None:
                usage = codex_usage_window(payload, metadata)
            if limit_reason is None:
                error_message, error_metadata = codex_error_details(payload)
                if error_metadata:
                    metadata.update(error_metadata)
                if error_message:
                    metadata.setdefault("error_message", error_message)
                    codex_limit = _classify_codex_usage_limit(error_message)
                    limit_reason = codex_limit.limit_reason if codex_limit else classify_execution_limit(error_message)
                    if codex_limit and codex_limit.retry_at:
                        metadata.setdefault("retry_at_hint", codex_limit.retry_at)
                    if codex_limit and codex_limit.purchase_more_credits:
                        metadata.setdefault("purchase_more_credits", True)
        return self.usage_observation_from_scan(
            execution,
            provider="openai",
            usage=usage,
            limit_reason=limit_reason,
            metadata=metadata,
            saw_payloads=bool(payloads),
            stderr_limit_extractor=codex_stderr_limit,
        )

    def stream_event_adapter(self):
        return codex_stream_event_adapter()

    def iter_native_payloads(self, stdout: str) -> list[dict[str, object]]:
        return iter_codex_payloads(stdout)

    def _iter_live_native_payloads(self, stdout: str) -> list[dict[str, object]]:
        return iter_codex_payloads(stdout, allow_incomplete_trailing=True)

    def translate_native_event(
        self,
        native_payload: dict[str, object],
    ) -> UnifiedEvent | None:
        return super().translate_native_event(native_payload)

    def extract_continuation(
        self,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        if execution is None or not execution.stdout.strip():
            return None
        for payload in iter_codex_payloads(execution.stdout):
            continuation = codex_continuation(payload)
            if continuation is not None:
                return continuation
        return None
