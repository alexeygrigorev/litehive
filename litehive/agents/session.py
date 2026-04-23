"""Session I/O mixin for SubagentManager."""

import json
import logging
import os
from pathlib import Path
import re
import signal
import time

from heru.base import CLIExecutionResult
from heru.types import LiveEvent, LiveTimeline, RuntimeEngineContinuation, SubagentRef, UnifiedEvent
from litehive.agents.artifacts import (
    write_stream_artifact,
    write_text_artifact,
)
from litehive.agents.session_store import (
    load_subagent_session,
    save_subagent_artifacts,
)
from litehive.domain.agent import SubagentInactivityTimeout
from litehive.domain.common import utcnow
from litehive.domain.runtime import ResourceLimitEvent
from litehive.domain.task import TaskRecord
from litehive.observability.events import append_event, append_session_log, ensure_session_log
from litehive.tasks.runtime import mark_subagent_pid
from pydantic import ValidationError

_OPENCODE_INACTIVITY_TIMEOUT_SECONDS = 300.0
_COMPLETED_INACTIVITY_PATTERN = re.compile(
    r"\[litehive\]\s*Process killed after\s+(?P<seconds>\d+(?:\.\d+)?)s of inactivity\.",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def _parse_unified_events(stdout: str) -> tuple[UnifiedEvent, ...]:
    events: list[UnifiedEvent] = []
    for line_number, raw_line in enumerate(stdout.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict) or "kind" not in payload:
            continue
        try:
            events.append(UnifiedEvent.model_validate(payload))
        except ValidationError as exc:
            logger.warning(
                "Skipping invalid unified event line %d while parsing subagent output: %s",
                line_number,
                exc,
            )
    return tuple(events)


def _render_event_for_transcript(event: UnifiedEvent) -> str:
    if event.kind in {"message", "status"} and event.content:
        return event.content
    if event.kind == "error" and event.error:
        return event.error
    if event.kind not in {"tool_call", "tool_result"}:
        return ""

    lines = ["```tool"]
    if event.tool_name:
        lines.append(f"name: {event.tool_name}")
    if event.tool_input:
        lines.append("input:")
        lines.append(event.tool_input.rstrip())
    if event.tool_output:
        lines.append("output:")
        lines.append(event.tool_output.rstrip())
    if event.error:
        lines.append("error:")
        lines.append(event.error.rstrip())
    lines.append("```")
    return "\n".join(lines)


def _render_transcript_from_events(events: tuple[UnifiedEvent, ...], *, stderr: str) -> str:
    parts = [rendered for event in events if (rendered := _render_event_for_transcript(event))]
    if not parts:
        return f"[stderr]\n{stderr.strip()}" if stderr.strip() else ""
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.strip()}")
    return "\n\n".join(parts)


def _continuation_from_events(events: tuple[UnifiedEvent, ...]) -> RuntimeEngineContinuation | None:
    continuation_id: str | None = None
    for event in events:
        if event.kind != "continuation":
            continue
        continuation_id = event.continuation_id or event.content or continuation_id
    if not continuation_id:
        return None
    return RuntimeEngineContinuation(session_id=continuation_id)


def _timeline_from_events(
    events: tuple[UnifiedEvent, ...],
    *,
    engine_name: str,
    task_id: str | None = None,
    subagent_id: str | None = None,
) -> LiveTimeline | None:
    if not events:
        return None
    timeline = LiveTimeline(engine=engine_name, task_id=task_id, subagent_id=subagent_id)
    timeline.events = [LiveEvent.model_validate(event.model_dump(mode="python")) for event in events]
    timeline.recompute_counts()
    return timeline


class SessionMixin:
    """Session I/O methods extracted from SubagentManager.

    Subclasses must provide: self.root, self.sandbox, self.config, self._stream_offsets.
    """

    @staticmethod
    def _render_execution_transcript(
        engine_name: str,
        execution: CLIExecutionResult | None,
    ) -> str:
        del engine_name
        if execution is None:
            return ""
        events = _parse_unified_events(execution.stdout)
        if not events:
            return execution.transcript
        return _render_transcript_from_events(events, stderr=execution.stderr)

    @staticmethod
    def _extract_execution_continuation(
        engine_name: str,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        del engine_name
        if execution is None:
            return None
        return _continuation_from_events(_parse_unified_events(execution.stdout))

    @staticmethod
    def _extract_execution_timeline(
        engine_name: str,
        stdout: str,
        *,
        task_id: str | None = None,
        subagent_id: str | None = None,
    ) -> LiveTimeline | None:
        return _timeline_from_events(
            _parse_unified_events(stdout),
            engine_name=engine_name,
            task_id=task_id,
            subagent_id=subagent_id,
        )

    def _append_stream_delta(self, base: Path, ref: SubagentRef, stream: str, full_content: str) -> None:
        """Append only the new portion of a stream to the append-only log."""
        key = f"{ref.id}:{stream}"
        prev = self._stream_offsets.get(key, 0)
        if len(full_content) > prev:
            append_session_log(base, stream, full_content[prev:])
            self._stream_offsets[key] = len(full_content)

    def _write_session_start(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
    ) -> None:
        ensure_session_log(base, "stdout")
        ensure_session_log(base, "stderr")
        append_event(
            self.root,
            task,
            "subagent_started",
            data={
                "subagent_id": ref.id,
                "role": ref.role,
                "engine": ref.engine,
                "sandboxed": ref.sandboxed,
            },
        )
        self._write_session_snapshot(
            task,
            base,
            ref,
            prompt=prompt,
            transcript="",
            stdout="",
            stderr="",
            report_payload={
                "status": ref.status,
                "summary": "",
                "files_changed": [],
                "tests": {"added": 0, "passing": 0},
                "warnings": [],
                "resource_control": self.sandbox.policy_summary(ref.engine, ref.role).as_dict(),
                "resource_limit_event": None,
            },
            exit_code=None,
            pid=None,
            interruption_reason=None,
            resource_limit_event=None,
        )

    def _write_session_metadata(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        *,
        exit_code: int | None,
        pid: int | None,
        interruption_reason: str | None = None,
        resource_limit_event: ResourceLimitEvent | None = None,
        continuation=None,
    ) -> None:
        created_at = utcnow()
        resource_control = self.sandbox.policy_summary(ref.engine, ref.role).as_dict()
        existing = load_subagent_session(self.root, task.id, ref.id)
        if isinstance(existing.get("created_at"), str):
            created_at = existing["created_at"]
        save_subagent_artifacts(
            self.root,
            task.id,
            ref.id,
            session={
                "id": ref.id,
                "role": ref.role,
                "engine": ref.engine,
                "status": ref.status,
                "sandboxed": ref.sandboxed,
                "sandbox": ref.sandbox_summary or "host",
                "created_at": created_at,
                "updated_at": utcnow(),
                "pid": pid,
                "exit_code": exit_code,
                "interruption_reason": interruption_reason,
                "resource_control": resource_control,
                "resource_limit_event": (
                    None if resource_limit_event is None else resource_limit_event.model_dump(mode="python")
                ),
                "continuation": None if continuation is None else continuation.model_dump(mode="python"),
            },
        )

    def _record_subagent_pid(self, task: TaskRecord, base: Path, ref: SubagentRef, pid: int | None) -> None:
        if pid is None:
            return
        mark_subagent_pid(self.root, task, pid)
        self._write_session_metadata(
            task,
            base,
            ref,
            exit_code=None,
            pid=pid,
            interruption_reason=None,
            resource_limit_event=None,
            continuation=None,
        )
        append_event(
            self.root,
            task,
            "subagent_pid",
            data={"subagent_id": ref.id, "pid": pid},
        )

    def _subagent_inactivity_timeout_seconds(self, engine_name: str) -> float:
        if engine_name == "opencode":
            return _OPENCODE_INACTIVITY_TIMEOUT_SECONDS
        return self.config.subagent_inactivity_timeout_seconds

    def _completed_inactivity_timeout(
        self,
        engine_name: str,
        execution: CLIExecutionResult,
    ) -> SubagentInactivityTimeout | None:
        match = _COMPLETED_INACTIVITY_PATTERN.search(execution.stderr or "")
        if match is None:
            return None
        limit_seconds = float(match.group("seconds"))
        return SubagentInactivityTimeout(
            execution,
            idle_seconds=limit_seconds,
            limit_seconds=limit_seconds,
        )

    def _check_stdout_inactivity(
        self,
        base: Path,
        engine_name: str,
        execution: CLIExecutionResult,
    ) -> None:
        if execution.pid is None:
            return
        stdout_path = base / "stdout.txt"
        if not stdout_path.exists():
            return
        limit_seconds = self._subagent_inactivity_timeout_seconds(engine_name)
        idle_seconds = max(0.0, time.time() - stdout_path.stat().st_mtime)
        if idle_seconds < limit_seconds:
            return
        self._terminate_stale_pid(execution.pid)
        raise SubagentInactivityTimeout(
            execution,
            idle_seconds=idle_seconds,
            limit_seconds=limit_seconds,
        )

    def _terminate_stale_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def _write_timeline(
        self,
        base: Path,
        ref: SubagentRef,
        task: TaskRecord,
        stdout: str,
    ) -> None:
        timeline = self._extract_execution_timeline(
            ref.engine,
            stdout,
            task_id=task.id,
            subagent_id=ref.id,
        )
        if timeline is None:
            return
        save_subagent_artifacts(
            self.root,
            task.id,
            ref.id,
            timeline=timeline.model_dump(mode="python"),
        )

    def _write_session_snapshot(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        *,
        prompt: str,
        transcript: str,
        stdout: str,
        stderr: str,
        report_payload: dict[str, object],
        exit_code: int | None,
        pid: int | None,
        interruption_reason: str | None,
        resource_limit_event: ResourceLimitEvent | None,
        continuation=None,
    ) -> None:
        created_at = utcnow()
        resource_control = self.sandbox.policy_summary(ref.engine, ref.role).as_dict()
        existing = load_subagent_session(self.root, task.id, ref.id)
        if isinstance(existing.get("created_at"), str):
            created_at = existing["created_at"]
        save_subagent_artifacts(
            self.root,
            task.id,
            ref.id,
            session={
                "id": ref.id,
                "role": ref.role,
                "engine": ref.engine,
                "status": ref.status,
                "sandboxed": ref.sandboxed,
                "sandbox": ref.sandbox_summary or "host",
                "created_at": created_at,
                "updated_at": utcnow(),
                "pid": pid,
                "exit_code": exit_code,
                "interruption_reason": interruption_reason,
                "resource_control": resource_control,
                "resource_limit_event": (
                    None if resource_limit_event is None else resource_limit_event.model_dump(mode="python")
                ),
                "continuation": None if continuation is None else continuation.model_dump(mode="python"),
            },
            report=report_payload,
        )
        write_text_artifact(base, "prompt", ".txt", prompt, compress=False)
        write_text_artifact(
            base,
            "transcript",
            ".md",
            transcript,
            compress=ref.status != "running",
        )
        write_stream_artifact(base, "stdout", stdout, compress=False)
        write_stream_artifact(base, "stderr", stderr, compress=False)
