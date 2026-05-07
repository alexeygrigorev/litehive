"""Session I/O collaborator for SubagentManager."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import signal
import time
from typing import TYPE_CHECKING

from heru import extract_engine_continuation
from heru.base import CLIExecutionResult
from heru.types import LiveTimeline, RuntimeEngineContinuation
from litehive.agents.execution_trace import (
    parse_unified_events,
    recovered_timeline_from_events,
    render_execution_trace_from_events,
)
from litehive.agents.artifacts import ArtifactService
from litehive.agents.session_store import (
    SubagentEventStreamPayload,
    save_subagent_artifacts,
)
from litehive.agents.session_events import SubagentPidEvent, SubagentStartedEvent
from litehive.agents.session_reports import SubagentReportPayload
from litehive.agents.session_snapshots import (
    RunningSubagentSessionRow,
    RunningSubagentSessionMetadata,
    SubagentSessionMetadata,
    SubagentSessionSnapshot,
    SubagentSessionStorageFields,
    TerminalSubagentSessionRow,
)
from litehive.domain.agent import SubagentInactivityTimeout
from litehive.domain.common import SubagentStatus, utcnow
from litehive.domain.runtime import Subagent
from litehive.domain.task import TaskRecord
from litehive.observability.events import append_session_log, ensure_session_log
from litehive.tasks.runtime import mark_subagent_pid

if TYPE_CHECKING:
    from litehive.sandbox.launcher import SandboxLauncher
    from litehive.config.model import LitehiveConfig
    from litehive.workspace import Workspace

_OPENCODE_INACTIVITY_TIMEOUT_SECONDS = 300.0
_COMPLETED_INACTIVITY_PATTERN = re.compile(
    r"\[litehive\]\s*Process killed after\s+(?P<seconds>\d+(?:\.\d+)?)s of inactivity\.",
    re.IGNORECASE,
)


@dataclass
class SubagentInactivityTimeoutPolicy:
    """
    Timeout rules for live and completed subagent executions.

    Live watchdog limits can vary by engine, while completed-process
    detection reads the stderr marker emitted by Heru's watchdog after
    the engine has already exited.
    """

    config: "LitehiveConfig"
    completed_marker: re.Pattern[str] = _COMPLETED_INACTIVITY_PATTERN

    def live_timeout_seconds(self, engine_name: str) -> float:
        """
        Return the stdout idle budget for one engine.
        """
        if engine_name == "opencode":
            return _OPENCODE_INACTIVITY_TIMEOUT_SECONDS
        return self.config.subagent_inactivity_timeout_seconds

    def completed_timeout(self, execution: CLIExecutionResult) -> SubagentInactivityTimeout | None:
        """
        Return a completed-run timeout when stderr carries the watchdog marker.
        """
        match = self.completed_marker.search(execution.stderr or "")
        if match is None:
            return None
        limit_seconds = float(match.group("seconds"))
        return SubagentInactivityTimeout(
            execution,
            idle_seconds=limit_seconds,
            limit_seconds=limit_seconds,
        )


@dataclass
class SubagentSessionManager:
    """
    Persist subagent session state and stream artifacts for one manager.

    ``SubagentManager`` delegates all session I/O here: initial
    snapshots, metadata-only updates, event-stream persistence,
    append-only stream deltas, PID recording, and stdout inactivity
    checks. Keeping these responsibilities in a concrete collaborator
    avoids inheritance while making the session boundary explicit.
    """

    root: Path
    workspace: "Workspace"
    sandbox: "SandboxLauncher"
    config: "LitehiveConfig"
    inactivity_policy: SubagentInactivityTimeoutPolicy
    _stream_offsets: dict[str, int] = field(default_factory=dict)

    def session_storage_fields(
        self,
        ref: Subagent,
        created_at: str,
        updated_at: str,
    ) -> SubagentSessionStorageFields:
        """
        Build the common typed session row fields for persistence.
        """
        return SubagentSessionStorageFields(
            id=ref.id,
            role=ref.role,
            engine=ref.engine,
            status=SubagentStatus(ref.status),
            sandboxed=ref.sandboxed,
            sandbox=ref.sandbox_summary or "host",
            created_at=created_at,
            updated_at=updated_at,
            resource_control=self.sandbox.policy_summary(ref.engine),
        )

    @staticmethod
    def render_execution_trace(
        engine_name: str,
        execution: CLIExecutionResult,
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
        events = parse_unified_events(execution.stdout)
        if not events:
            return execution.transcript
        return render_execution_trace_from_events(events, stderr=execution.stderr)

    @staticmethod
    def extract_execution_continuation(
        engine_name: str,
        execution: CLIExecutionResult,
    ) -> RuntimeEngineContinuation | None:
        """Pull the engine-specific resume token from the run result so retry and continuation flows can hand it back to the engine on the next turn."""
        return extract_engine_continuation(engine_name, execution)

    @staticmethod
    def extract_execution_event_stream(
        engine_name: str,
        stdout: str,
        task_id: str | None = None,
        subagent_id: str | None = None,
    ) -> LiveTimeline | None:
        """Parse stdout into the live event timeline persisted as the subagent's ``event_stream`` artifact, so the status UI can replay tool calls without re-parsing raw stdout each time."""
        return recovered_timeline_from_events(
            parse_unified_events(stdout),
            engine_name=engine_name,
            task_id=task_id,
            subagent_id=subagent_id,
        )

    def append_stream_delta(self, base: Path, ref: Subagent, stream: str, full_content: str) -> None:
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
        ref: Subagent,
        prompt: str,
    ) -> None:
        """Lay down the empty stdout/stderr logs and the initial running snapshot.

        Called by SubagentManager just before launching the engine process so
        that observers (status snapshots, recovery) can see a `running` session
        even if the launch crashes before any output arrives.
        """
        ensure_session_log(base, "stdout")
        ensure_session_log(base, "stderr")
        self.workspace.append_event(
            task,
            SubagentStartedEvent(
                subagent_id=ref.id,
                role=ref.role,
                engine=ref.engine,
                sandboxed=ref.sandboxed,
            ),
        )
        self.write_session_snapshot(
            task,
            base,
            ref,
            snapshot=SubagentSessionSnapshot(
                prompt=prompt,
                transcript="",
                stdout="",
                stderr="",
                report=SubagentReportPayload(
                    status=SubagentStatus(ref.status),
                    summary="",
                    tests={"added": 0, "passing": 0},
                    resource_control=self.sandbox.policy_summary(ref.engine),
                ),
                metadata=SubagentSessionMetadata(exit_code=None, pid=None),
            ),
        )

    def write_running_session_metadata(
        self,
        task: TaskRecord,
        ref: Subagent,
        metadata: RunningSubagentSessionMetadata,
    ) -> None:
        """
        Update running-session metadata without touching report or
        stream artifacts.

        Metadata-only writes happen while the subagent is still
        running: PID assignment and live continuation updates. Exit
        codes and interruption reasons are terminal state and are
        written through full session snapshots.
        """
        created_at = self.workspace.load_subagent_session_created_at(task.id, ref.id) or utcnow()
        session_row = RunningSubagentSessionRow(
            fields=self.session_storage_fields(ref, created_at, utcnow()),
            pid=metadata.pid,
            continuation=metadata.continuation,
        )
        save_subagent_artifacts(
            self.workspace,
            task.id,
            ref.id,
            session=session_row,
        )

    def record_subagent_pid(self, task: TaskRecord, ref: Subagent, pid: int | None) -> None:
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
        self.write_running_session_metadata(
            task,
            ref,
            metadata=RunningSubagentSessionMetadata(pid=pid),
        )
        self.workspace.append_event(
            task,
            SubagentPidEvent(subagent_id=ref.id, role=ref.role, pid=pid),
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
        return self.inactivity_policy.live_timeout_seconds(engine_name)

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
        return self.inactivity_policy.completed_timeout(execution)

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
        ref: Subagent,
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
            event_stream=SubagentEventStreamPayload(event_stream.model_dump(mode="python")),
        )

    def write_session_snapshot(
        self,
        task: TaskRecord,
        base: Path,
        ref: Subagent,
        snapshot: SubagentSessionSnapshot,
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
        created_at = self.workspace.load_subagent_session_created_at(task.id, ref.id) or utcnow()
        session_row = self.session_row_for_snapshot(ref, snapshot, created_at)
        save_subagent_artifacts(
            self.workspace,
            task.id,
            ref.id,
            session=session_row,
            report=snapshot.report,
        )
        artifacts = ArtifactService(base)
        artifacts.write_text("prompt", ".txt", snapshot.prompt, compress=False)
        if ref.status == SubagentStatus.RUNNING:
            artifacts.remove_text("execution_trace", ".md")
        else:
            artifacts.write_text("execution_trace", ".md", snapshot.transcript, compress=True)
        artifacts.write_stream("stdout", snapshot.stdout, compress=False)
        artifacts.write_stream("stderr", snapshot.stderr, compress=False)

    def session_row_for_snapshot(
        self,
        ref: Subagent,
        snapshot: SubagentSessionSnapshot,
        created_at: str,
    ) -> RunningSubagentSessionRow | TerminalSubagentSessionRow:
        """
        Convert a complete snapshot into the concrete persisted row type.
        """
        fields = self.session_storage_fields(ref, created_at, utcnow())
        continuation = snapshot.metadata.continuation
        if snapshot.metadata.exit_code is None:
            return RunningSubagentSessionRow(
                fields=fields,
                pid=snapshot.metadata.pid,
                continuation=continuation,
            )
        return TerminalSubagentSessionRow(
            fields=fields,
            exit_code=snapshot.metadata.exit_code,
            pid=snapshot.metadata.pid,
            interruption_reason=snapshot.metadata.interruption_reason,
            continuation=continuation,
        )
