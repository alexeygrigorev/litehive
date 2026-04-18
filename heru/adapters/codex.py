"""Public Codex adapter module."""

import logging
from pathlib import Path

from heru.adapters._codex_impl import (
    classify_codex_usage_limit as _classify_codex_usage_limit,
    codex_continuation,
    codex_error_details,
    extract_codex_errors,
    iter_codex_payloads,
    codex_stderr_limit,
    codex_stream_event_adapter,
    codex_usage_window,
)
from heru.base import ExternalCLIAdapter


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
    SUPPORTS_CONTINUE_LATEST = True
    TRANSCRIPT_EMPTY_ON_PARSED_PAYLOADS = True
    USAGE_PROVIDER = "openai"

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
        if latest_resume:
            command.append("--last")
        elif resume_session_id:
            command.append(resume_session_id)
        command.append(prompt)
        return command

    def transcript_assistant_text(self, execution) -> str:
        return _extract_codex_transcript(execution.stdout)

    def transcript_error_text(self, execution) -> str:
        return "\n".join(extract_codex_errors(execution.stdout)).strip()

    def usage_window_from_payload(self, payload, metadata):
        return codex_usage_window(payload, metadata)

    def error_details_from_payload(self, payload):
        error_message, error_metadata = codex_error_details(payload)
        return error_message, error_metadata, None

    def classify_limit_text(self, text, metadata):
        codex_limit = _classify_codex_usage_limit(text)
        if codex_limit is not None:
            if codex_limit.retry_at:
                metadata.setdefault("retry_at_hint", codex_limit.retry_at)
            if codex_limit.purchase_more_credits:
                metadata.setdefault("purchase_more_credits", True)
            return codex_limit.limit_reason
        return super().classify_limit_text(text, metadata)

    def classify_stderr_limit(self, stderr, metadata):
        return codex_stderr_limit(stderr, metadata)

    def stream_event_adapter(self):
        return codex_stream_event_adapter()

    def iter_native_payloads(self, stdout: str) -> list[dict[str, object]]:
        return iter_codex_payloads(stdout)

    def _iter_live_native_payloads(self, stdout: str) -> list[dict[str, object]]:
        return iter_codex_payloads(stdout, allow_incomplete_trailing=True)

    def continuation_from_payload(self, payload):
        return codex_continuation(payload)
