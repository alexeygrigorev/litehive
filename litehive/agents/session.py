"""Session I/O mixin for SubagentManager."""

import os
from pathlib import Path
import re
import signal
import time

from heru import extract_engine_continuation
from heru.base import CLIExecutionResult
from heru.types import LiveTimeline as LiveEventStream, RuntimeEngineContinuation, SubagentRef
from litehive.agents.execution_trace import (
    event_stream_from_events,
    parse_unified_events,
    render_execution_trace_from_events,
)
from litehive.agents.artifacts import (
    remove_text_artifact,
    write_stream_artifact,
    write_text_artifact,
)
from litehive.agents.session_store import (
    load_subagent_session,
    save_subagent_artifacts,
)
from litehive.domain.agent import SubagentInactivityTimeout
from litehive.domain.common import utcnow
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

    Subclasses must provide: self.root, self.workspace, self.sandbox, self.config, self._stream_offsets.
    """

    @staticmethod
    def render_execution_trace(
        engine_name: str,
        execution: CLIExecutionResult | None,
    ) -> str:
        """
        Produce the human-readable transcript saved alongside each
        subagent run.

        Falls back to the engine's raw transcript when the
        unified-event parse yields nothing so the SubagentManager
        always has something to write to ``execution_trace.md`` —
        without this fallback, an engine that fails to emit unified
        events would leave the transcript artifact empty.
        """
        del engine_name
        if execution is None:
            return ""
        events = parse_unified_events(execution.stdout)
        if not events:
            return execution.transcript
        return render_execution_trace_from_events(events, stderr=execution.stderr)

    @staticmethod
    def extract_execution_continuation(
        engine_name: str,
        execution: CLIExecutionResult | None,
    ) -> RuntimeEngineContinuation | None:
        """Pull the engine-specific resume token from the run result so retry and continuation flows can hand it back to the engine on the next turn."""
        return extract_engine_continuation(engine_name, execution)

    @staticmethod
    def extract_execution_event_stream(
        engine_name: str,
        stdout: str,
        task_id: str | None = None,
        subagent_id: str | None = None,
    ) -> LiveEventStream | None:
        """Parse stdout into the live event timeline persisted as the subagent's ``event_stream`` artifact, so the status UI can replay tool calls without re-parsing raw stdout each time."""
        return event_stream_from_events(
            parse_unified_events(stdout),
            engine_name=engine_name,
            task_id=task_id,
            subagent_id=subagent_id,
        )

    def append_stream_delta(self, base: Path, ref: SubagentRef, stream: str, full_content: str) -> None:
        """
        Append only the new portion of a stream to the append-only log.

        The session writes the full transcript on every progress
        callback, but the append-only log should grow incrementally;
        ``_stream_offsets`` tracks how much of each stream has
        already been logged so we never double-write or rewrite
        history.
        """
        key = f"{ref.id}:{stream}"
        prev = self._stream_offsets.get(key, 0)
        if len(full_content) > prev:
            append_session_log(base, stream, full_content[prev:])
            self._stream_offsets[key] = len(full_content)

    def write_session_start(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
    ) -> None:
        """Lay down the empty stdout/stderr logs and the initial running snapshot.

        Called by SubagentManager just before launching the engine process so
        that observers (status snapshots, recovery) can see a `running` session
        even if the launch crashes before any output arrives.
        """
        ensure_session_log(base, "stdout")
        ensure_session_log(base, "stderr")
        append_event(
            self.workspace,
            task,
            "subagent_started",
            data={
                "subagent_id": ref.id,
                "role": ref.role,
                "engine": ref.engine,
                "sandboxed": ref.sandboxed,
            },
        )
        self.write_session_snapshot(
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
            },
            exit_code=None,
            pid=None,
            interruption_reason=None,
        )

    def write_session_metadata(
        self,
        task: TaskRecord,
        ref: SubagentRef,
        exit_code: int | None,
        pid: int | None,
        interruption_reason: str | None = None,
        continuation=None,
    ) -> None:
        """
        Update the session row without touching the report or stream
        artifacts.

        Used by SubagentManager for metadata-only events (PID
        assignment, interruption reason) where rewriting the report
        payload would clobber the last good values from the engine's
        own output. The selective update is what keeps a metadata
        write from undoing a progress snapshot.
        """
        created_at = utcnow()
        resource_control = self.sandbox.policy_summary(ref.engine, ref.role).as_dict()
        existing = load_subagent_session(self.workspace, task.id, ref.id)
        if isinstance(existing.get("created_at"), str):
            created_at = existing["created_at"]
        if continuation is None:
            continuation_payload = None
        else:
            continuation_payload = continuation.model_dump(mode="python")
        save_subagent_artifacts(
            self.workspace,
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
                "continuation": continuation_payload,
            },
        )

    def record_subagent_pid(self, task: TaskRecord, ref: SubagentRef, pid: int | None) -> None:
        """
        Pin the engine PID into the runtime row, session metadata,
        and event log.

        The recovery flow keys off this PID to decide whether a
        crashed subagent's process is still alive, so it must land in
        all three places before the engine starts producing real
        output — without all three writes, recovery sees an
        inconsistent picture and may misclassify a still-running
        subagent as crashed.
        """
        if pid is None:
            return
        mark_subagent_pid(self.root, task, pid)
        self.write_session_metadata(
            task,
            ref,
            exit_code=None,
            pid=pid,
            interruption_reason=None,
            continuation=None,
        )
        append_event(
            self.workspace,
            task,
            "subagent_pid",
            data={"subagent_id": ref.id, "pid": pid},
        )

    def subagent_inactivity_timeout_seconds(self, engine_name: str) -> float:
        """
        Return the stdout idle budget the watchdog enforces for an
        engine.

        Opencode legitimately stalls for long stretches between tool
        calls and needs a wider window than the global default; this
        lookup keeps the engine-specific exception in one place
        instead of scattering hard-coded exceptions across the watchdog.
        """
        if engine_name == "opencode":
            return _OPENCODE_INACTIVITY_TIMEOUT_SECONDS
        return self.config.subagent_inactivity_timeout_seconds

    def completed_inactivity_timeout(
        self,
        execution: CLIExecutionResult,
    ) -> SubagentInactivityTimeout | None:
        """
        Detect a watchdog-killed run from its stderr marker after the
        process has already exited.

        When the inline watchdog ends a stalled engine the process
        returns normally; without this scrape the SubagentManager
        would treat the truncated transcript as a clean run, lose the
        timeout signal, and let the lifecycle accept whatever partial
        output remained.
        """
        match = _COMPLETED_INACTIVITY_PATTERN.search(execution.stderr or "")
        if match is None:
            return None
        limit_seconds = float(match.group("seconds"))
        return SubagentInactivityTimeout(
            execution,
            idle_seconds=limit_seconds,
            limit_seconds=limit_seconds,
        )

    def check_stdout_inactivity(
        self,
        base: Path,
        engine_name: str,
        execution: CLIExecutionResult,
    ) -> None:
        """
        Kill an engine that has stopped producing stdout, then raise
        the timeout to the caller.

        Runs after each streaming poll in SubagentManager so a hung
        engine cannot quietly burn the whole task budget while
        emitting no output; the kill is best-effort and the raise is
        what stops the polling loop.
        """
        if execution.pid is None:
            return
        stdout_path = base / "stdout.txt"
        if not stdout_path.exists():
            return
        limit_seconds = self.subagent_inactivity_timeout_seconds(engine_name)
        idle_seconds = max(0.0, time.time() - stdout_path.stat().st_mtime)
        if idle_seconds < limit_seconds:
            return
        self.terminate_stale_pid(execution.pid)
        raise SubagentInactivityTimeout(
            execution,
            idle_seconds=idle_seconds,
            limit_seconds=limit_seconds,
        )

    def terminate_stale_pid(self, pid: int) -> None:
        """
        Best-effort SIGTERM on a stale subagent pid.

        Swallows the race where the engine already exited between the
        watchdog's "is it idle" check and this kill — losing the kill
        is fine because the process is already gone.
        """
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def write_event_stream(
        self,
        ref: SubagentRef,
        task: TaskRecord,
        stdout: str,
    ) -> None:
        """Persist the parsed event timeline so live status views can replay tool calls without re-parsing raw stdout on every render."""
        event_stream = self.extract_execution_event_stream(
            ref.engine,
            stdout,
            task_id=task.id,
            subagent_id=ref.id,
        )
        if event_stream is None:
            return
        save_subagent_artifacts(
            self.workspace,
            task.id,
            ref.id,
            event_stream=event_stream.model_dump(mode="python"),
        )

    def write_session_snapshot(
        self,
        task: TaskRecord,
        base: Path,
        ref: SubagentRef,
        prompt: str,
        transcript: str,
        stdout: str,
        stderr: str,
        report_payload: dict[str, object],
        exit_code: int | None,
        pid: int | None,
        interruption_reason: str | None,
        continuation=None,
    ) -> None:
        """
        Write a complete subagent snapshot in one call.

        Covers the session row, report, prompt, transcript, and
        stream artifacts; this is the single fan-out point used after
        a run finishes (success or failure) so that every observer
        surface — CLI status, recovery diagnostics, retrospective
        debugging — sees a consistent set of artifacts instead of
        catching the snapshot mid-update.
        """
        created_at = utcnow()
        resource_control = self.sandbox.policy_summary(ref.engine, ref.role).as_dict()
        existing = load_subagent_session(self.workspace, task.id, ref.id)
        if isinstance(existing.get("created_at"), str):
            created_at = existing["created_at"]
        if continuation is None:
            continuation_payload = None
        else:
            continuation_payload = continuation.model_dump(mode="python")
        save_subagent_artifacts(
            self.workspace,
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
                "continuation": continuation_payload,
            },
            report=report_payload,
        )
        write_text_artifact(base, "prompt", ".txt", prompt, compress=False)
        if ref.status == "running":
            remove_text_artifact(base, "execution_trace", ".md")
        else:
            write_text_artifact(base, "execution_trace", ".md", transcript, compress=True)
        write_stream_artifact(base, "stdout", stdout, compress=False)
        write_stream_artifact(base, "stderr", stderr, compress=False)
