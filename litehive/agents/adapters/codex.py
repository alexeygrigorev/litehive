"""Codex CLI engine adapter."""

import logging
from pathlib import Path

from litehive.agents.adapters.common import classify_execution_limit
from litehive.agents.adapters._codex_impl import (
    classify_codex_usage_limit as _classify_codex_usage_limit,
    codex_continuation,
    codex_error_details,
    codex_stderr_limit,
    codex_stream_event_adapter,
    codex_usage_window,
)
from litehive.agents.base import (
    CLIExecutionResult,
    ExternalCLIAdapter,
    extract_codex_errors,
    extract_codex_messages,
    iter_jsonl_payloads,
    parse_stage_report_text,
)
from litehive.models import RuntimeEngineContinuation


logger = logging.getLogger(__name__)


def _extract_codex_transcript(stdout: str) -> str:
    messages: dict[str, str] = {}
    for payload in iter_jsonl_payloads(stdout):
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
    DEFAULT_NAME = "codex"
    DEFAULT_BINARY = "codex"
    DEFAULT_CAPABILITIES = ExternalCLIAdapter.DEFAULT_CAPABILITIES.__class__(
        supports_model_override=False,
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
        if resume_session_id:
            prompt = f"[Resuming prior session {resume_session_id}]\n\n{prompt}"
        command = [
            self.binary,
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(cwd),
            "--skip-git-repo-check",
        ]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self.render_transcript_from_parts(
            execution,
            assistant_text=extract_codex_messages(execution.stdout),
            error_text="\n".join(extract_codex_errors(execution.stdout)).strip(),
            empty_on_parsed_payloads=True,
        )

    def parse_stage_report(self, *, task_id: str, step: str, execution: CLIExecutionResult, subagent_status: str):
        transcript = self.render_transcript(execution)
        if not transcript:
            transcript = "\n".join(extract_codex_errors(execution.stdout))
        return parse_stage_report_text(task_id=task_id, step=step, transcript=transcript, subagent_status=subagent_status)

    def extract_usage_observation(self, execution: CLIExecutionResult):
        payloads = iter_jsonl_payloads(execution.stdout)
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

    def extract_continuation(
        self,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        return self.extract_continuation_from_payloads(execution, codex_continuation)
