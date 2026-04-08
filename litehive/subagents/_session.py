"""Session I/O mixin for SubagentManager."""

import os
from pathlib import Path
import signal
import time

import yaml

from litehive.engines import extract_engine_timeline
from litehive.engines.base import CLIExecutionResult
from litehive.events import append_event, append_session_log, ensure_session_log
from litehive.models import ResourceLimitEvent, SubagentRef, TaskRecord, utcnow
from litehive.subagents._artifacts import (
    _write_stream_artifact,
    _write_text_artifact,
)
from litehive.subagents._models import SubagentInactivityTimeout
from litehive.tasks import _write_atomic_files, mark_subagent_pid


class _SessionMixin:
    """Session I/O methods extracted from SubagentManager.

    Subclasses must provide: self.root, self.sandbox, self.config, self._stream_offsets.
    """

    def _append_stream_delta(
        self, base: Path, ref: SubagentRef, stream: str, full_content: str
    ) -> None:
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
                "resource_control": self.sandbox.policy_summary(ref.engine).as_dict(),
                "resource_limit_event": None,
            },
            exit_code=None,
            pid=None,
            interruption_reason=None,
            resource_limit_event=None,
        )

    def _write_session_metadata(
        self,
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
        session_path = base / "session.yaml"
        resource_control = self.sandbox.policy_summary(ref.engine).as_dict()
        if session_path.exists():
            existing = yaml.safe_load(session_path.read_text(encoding="utf-8")) or {}
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        _write_atomic_files(
            {
                session_path: yaml.safe_dump(
                    {
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
                            None
                            if resource_limit_event is None
                            else resource_limit_event.model_dump(mode="python")
                        ),
                        "continuation": None
                        if continuation is None
                        else continuation.model_dump(mode="python"),
                    },
                    sort_keys=False,
                )
            }
        )

    def _record_subagent_pid(
        self, task: TaskRecord, base: Path, ref: SubagentRef, pid: int | None
    ) -> None:
        if pid is None:
            return
        mark_subagent_pid(self.root, task, pid)
        self._write_session_metadata(
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

    def _check_stdout_inactivity(self, base: Path, execution: CLIExecutionResult) -> None:
        if execution.pid is None:
            return
        stdout_path = base / "stdout.txt"
        if not stdout_path.exists():
            return
        idle_seconds = max(0.0, time.time() - stdout_path.stat().st_mtime)
        if idle_seconds < self.config.subagent_inactivity_timeout_seconds:
            return
        self._terminate_stale_pid(execution.pid)
        raise SubagentInactivityTimeout(
            execution,
            idle_seconds=idle_seconds,
            limit_seconds=self.config.subagent_inactivity_timeout_seconds,
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
        timeline = extract_engine_timeline(
            ref.engine,
            stdout,
            task_id=task.id,
            subagent_id=ref.id,
        )
        if timeline is None:
            return
        _write_text_artifact(
            base,
            "timeline",
            ".yaml",
            yaml.safe_dump(timeline.model_dump(mode="python"), sort_keys=False),
            compress=ref.status != "running",
        )

    def _write_session_snapshot(
        self,
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
        session_path = base / "session.yaml"
        resource_control = self.sandbox.policy_summary(ref.engine).as_dict()
        if session_path.exists():
            existing = yaml.safe_load(session_path.read_text(encoding="utf-8")) or {}
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        _write_atomic_files(
            {
                session_path: yaml.safe_dump(
                    {
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
                            None
                            if resource_limit_event is None
                            else resource_limit_event.model_dump(mode="python")
                        ),
                        "continuation": None
                        if continuation is None
                        else continuation.model_dump(mode="python"),
                    },
                    sort_keys=False,
                ),
                base / "report.yaml": yaml.safe_dump(report_payload, sort_keys=False),
            }
        )
        _write_text_artifact(base, "prompt", ".txt", prompt, compress=False)
        _write_text_artifact(
            base,
            "transcript",
            ".md",
            transcript,
            compress=ref.status != "running",
        )
        _write_stream_artifact(base, "stdout", stdout, compress=False)
        _write_stream_artifact(base, "stderr", stderr, compress=False)
