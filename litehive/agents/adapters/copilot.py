"""Copilot CLI engine adapter."""

from pathlib import Path

from litehive.agents.base import (
    CLIExecutionResult,
    ExternalCLIAdapter,
    StreamEventAdapter,
    extract_stream_errors,
    extract_stream_transcript,
    iter_jsonl_payloads,
    parse_stage_report_text,
)
from litehive.models import (
    EngineUsageObservation,
    EngineUsageWindow,
    LiveEvent,
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
        assistant_text = extract_stream_transcript(
            execution.stdout,
            adapter=self.stream_event_adapter(),
        )
        if assistant_text:
            if execution.stderr.strip():
                return f"{assistant_text}\n\n[stderr]\n{execution.stderr.strip()}"
            return assistant_text
        return execution.transcript

    def parse_stage_report(
        self,
        *,
        task_id: str,
        step: str,
        execution: CLIExecutionResult,
        subagent_status: str,
    ):
        transcript = self.render_transcript(execution)
        if transcript == execution.transcript:
            error_lines = extract_stream_errors(
                execution.stdout,
                adapter=self.stream_event_adapter(),
            )
            if error_lines:
                transcript = "\n".join(error_lines)
        return parse_stage_report_text(
            task_id=task_id,
            step=step,  # type: ignore[arg-type]
            transcript=transcript,
            subagent_status=subagent_status,  # type: ignore[arg-type]
        )

    def extract_usage_observation(
        self,
        execution: CLIExecutionResult,
    ) -> EngineUsageObservation | None:
        payloads = iter_jsonl_payloads(execution.stdout)
        for payload in reversed(payloads):
            if payload.get("type") != "assistant.usage":
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                continue
            metadata: dict[str, str | int | bool | None] = {}
            total_tokens = 0
            saw_tokens = False
            for field in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
                raw_value = data.get(field)
                if isinstance(raw_value, int):
                    metadata[field] = raw_value
                    total_tokens += raw_value
                    saw_tokens = True
            if isinstance(data.get("model"), str) and data["model"]:
                metadata["model"] = data["model"]
            if isinstance(data.get("cost"), (int, float)):
                metadata["cost"] = str(data["cost"])
            usage = EngineUsageWindow(used=total_tokens, unit="tokens") if saw_tokens else None

            quota_snapshots = data.get("quotaSnapshots")
            if isinstance(quota_snapshots, dict) and quota_snapshots:
                selected_name, selected_snapshot = _select_copilot_quota_snapshot(quota_snapshots)
                if isinstance(selected_snapshot, dict):
                    quota_usage = _copilot_quota_usage_window(selected_snapshot)
                    if quota_usage is not None:
                        usage = quota_usage
                    metadata["quota_snapshot"] = selected_name
                    for field in (
                        "isUnlimitedEntitlement",
                        "entitlementRequests",
                        "usedRequests",
                        "usageAllowedWithExhaustedQuota",
                        "overage",
                        "overageAllowedWithExhaustedQuota",
                        "remainingPercentage",
                    ):
                        raw_value = selected_snapshot.get(field)
                        if isinstance(raw_value, (bool, int)):
                            metadata[field] = raw_value
                        elif isinstance(raw_value, float):
                            metadata[field] = f"{raw_value:.6f}"
                    reset_date = selected_snapshot.get("resetDate")
                    if isinstance(reset_date, str) and reset_date:
                        metadata["resetDate"] = reset_date

            return EngineUsageObservation(
                source="provider",
                provider="github",
                success=execution.exit_code == 0,
                usage=usage,
                metadata=metadata,
            )
        return None
    def stream_event_adapter(self) -> StreamEventAdapter:
        return StreamEventAdapter(
            final_messages=self._final_messages,
            text_deltas=self._text_deltas,
            errors=self._errors,
            live_events=self._live_events,
        )

    @staticmethod
    def _live_events(payload: dict[str, object]) -> list[LiveEvent]:
        events: list[LiveEvent] = []
        event_type = payload.get("type")
        data = payload.get("data")
        if event_type == "assistant.message":
            if isinstance(data, dict):
                content = data.get("content")
                if isinstance(content, str) and content:
                    events.append(
                        LiveEvent(kind="message", engine="copilot", role="assistant", content=content)
                    )
        elif event_type == "assistant.message_delta":
            if isinstance(data, dict):
                content = data.get("deltaContent")
                if isinstance(content, str) and content:
                    events.append(
                        LiveEvent(kind="message", engine="copilot", role="assistant", content=content)
                    )
        elif event_type == "tool.execution_start":
            if isinstance(data, dict):
                tool_name = data.get("toolName") or data.get("tool")
                events.append(
                    LiveEvent(
                        kind="tool_call",
                        engine="copilot",
                        role="assistant",
                        tool_name=tool_name if isinstance(tool_name, str) else None,
                    )
                )
        elif event_type == "tool.execution_complete":
            if isinstance(data, dict):
                tool_name = data.get("toolName") or data.get("tool")
                result = data.get("result")
                tool_output = None
                if isinstance(result, str):
                    tool_output = result
                elif isinstance(result, dict):
                    content = result.get("content") or result.get("detailedContent")
                    if isinstance(content, str):
                        tool_output = content
                events.append(
                    LiveEvent(
                        kind="tool_result",
                        engine="copilot",
                        role="user",
                        tool_name=tool_name if isinstance(tool_name, str) else None,
                        tool_output=tool_output,
                    )
                )
        elif event_type == "assistant.usage":
            if isinstance(data, dict):
                meta: dict[str, str | int | bool | None] = {}
                for field in ("inputTokens", "outputTokens"):
                    raw = data.get(field)
                    if isinstance(raw, int):
                        meta[field] = raw
                model = data.get("model")
                if isinstance(model, str):
                    meta["model"] = model
                if meta:
                    events.append(LiveEvent(kind="usage", engine="copilot", metadata=meta))
        elif event_type == "error":
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                events.append(LiveEvent(kind="error", engine="copilot", error=data["message"]))
        return events


    @staticmethod
    def _final_messages(payload: dict[str, object]) -> list[str]:
        if payload.get("type") != "assistant.message":
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        content = data.get("content")
        if isinstance(content, str) and content:
            return [content]
        return []

    @staticmethod
    def _text_deltas(payload: dict[str, object]) -> list[tuple[int, str]]:
        if payload.get("type") != "assistant.message_delta":
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        content = data.get("deltaContent")
        if isinstance(content, str) and content:
            return [(0, content)]
        return []

    @staticmethod
    def _errors(payload: dict[str, object]) -> list[str]:
        event_type = payload.get("type")
        if event_type == "error":
            data = payload.get("data")
            if isinstance(data, dict):
                message = data.get("message")
                if isinstance(message, str) and message:
                    return [message]
            return []
        if event_type == "tool.execution_complete":
            data = payload.get("data")
            if not isinstance(data, dict) or data.get("success", True):
                return []
            result = data.get("result")
            if isinstance(result, dict):
                content = result.get("content") or result.get("detailedContent")
                if isinstance(content, str) and content:
                    return [content]
            elif isinstance(result, str) and result:
                return [result]
        return []


def _copilot_quota_usage_window(snapshot: dict[str, object]) -> EngineUsageWindow | None:
    entitlement_requests = snapshot.get("entitlementRequests")
    used_requests = snapshot.get("usedRequests")
    if not isinstance(used_requests, int):
        return None
    remaining: int | None = None
    if isinstance(entitlement_requests, int):
        remaining = max(entitlement_requests - used_requests, 0)
    reset_date = snapshot.get("resetDate")
    return EngineUsageWindow(
        used=used_requests,
        limit=entitlement_requests if isinstance(entitlement_requests, int) else None,
        remaining=remaining,
        unit="requests",
        reset_at=reset_date if isinstance(reset_date, str) and reset_date else None,
    )


def _select_copilot_quota_snapshot(
    snapshots: dict[object, object],
) -> tuple[str, dict[str, object] | None]:
    preferred_names = ("premium_interactions", "chat", "completions")
    normalized: dict[str, dict[str, object]] = {
        str(name): value for name, value in snapshots.items() if isinstance(value, dict)
    }
    for name in preferred_names:
        selected = normalized.get(name)
        if selected is not None:
            return name, selected
    for name, snapshot in normalized.items():
        return name, snapshot
    return "", None
