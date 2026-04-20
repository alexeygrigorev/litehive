"""Session I/O mixin for SubagentManager."""

import os
from pathlib import Path
import re
import signal
import time
from typing import Callable

from heru.base import CLIExecutionResult
from heru.types import SubagentRef
from litehive.agents.artifacts import (
    write_stream_artifact,
    write_text_artifact,
)
from litehive.heru_compat import (
    extract_engine_continuation,
    extract_engine_timeline,
    render_execution_transcript,
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

_OPENCODE_INACTIVITY_TIMEOUT_SECONDS = 300.0
_COMPLETED_INACTIVITY_PATTERN = re.compile(
    r"\[litehive\]\s*Process killed after\s+(?P<seconds>\d+(?:\.\d+)?)s of inactivity\.",
    re.IGNORECASE,
)


class SessionMixin:
    """Session I/O methods extracted from SubagentManager.

    Subclasses must provide: self.root, self.sandbox, self.config, self._stream_offsets.
    """

    @staticmethod
    def _render_execution_transcript(
        engine_name: str,
        execution: CLIExecutionResult | None,
        *,
        fallback_renderer: Callable[[CLIExecutionResult], str] | None = None,
    ) -> str:
        return render_execution_transcript(
            engine_name,
            execution,
            fallback_renderer=fallback_renderer,
        )

    @staticmethod
    def _extract_execution_continuation(
        engine_name: str,
        execution: CLIExecutionResult | None,
    ):
        return extract_engine_continuation(engine_name, execution)

    @staticmethod
    def _extract_execution_timeline(
        engine_name: str,
        stdout: str,
        *,
        task_id: str | None = None,
        subagent_id: str | None = None,
    ):
        return extract_engine_timeline(
            engine_name,
            stdout,
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
