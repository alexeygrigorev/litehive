"""Engine callback wrappers for subagent runs."""

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Protocol

from heru.base import CLIExecutionResult

from litehive.domain.runtime import Subagent
from litehive.domain.task import TaskRecord

logger = logging.getLogger(__name__)


class SubagentPidRecorder(Protocol):
    """
    Object that can persist the live PID for a subagent.

    ``SubagentSessionManager`` implements this in production; tests
    can pass small fakes without inheriting the concrete session
    manager.
    """

    def record_subagent_pid(self, task: TaskRecord, ref: Subagent, pid: int | None, /) -> None: ...


class ProgressSnapshotWriter(Protocol):
    """
    Object that can persist one live progress snapshot.

    ``SubagentManager`` implements this today. The callback wrapper only
    needs this narrow method, which keeps callback error handling
    decoupled from the rest of the manager surface.
    """

    def write_session_progress(
        self,
        task: TaskRecord,
        base: Path,
        ref: Subagent,
        prompt: str,
        execution: CLIExecutionResult,
        /,
    ) -> None: ...


@dataclass
class CallbackWarnings:
    """
    Collect non-fatal callback bookkeeping warnings for one subagent run.

    The live ``on_started`` and ``on_update`` callbacks record failures
    here instead of mutating a shared list directly; finish snapshots
    then merge the collected warnings into either the parsed
    ``StageReport`` or the missing-verdict placeholder report.
    """

    _warnings: list[str] = field(default_factory=list)

    def record_failure(self, ref: Subagent, phase: str, exc: Exception) -> None:
        """
        Add one callback failure warning and log the original exception.

        Callback persistence is best-effort: a SQLite/filesystem
        failure in the callback should be visible to the operator but
        must not crash the running engine process.
        """
        warning = f"runner {phase} bookkeeping failed: {type(exc).__name__}: {exc}"
        if warning not in self._warnings:
            self._warnings.append(warning)
        logger.exception(
            "Subagent %s %s callback failed; continuing without crashing the runner",
            ref.id,
            phase,
        )

    def merged_with(self, base: list[str]) -> list[str]:
        """
        Return ``base`` plus collected callback warnings, deduped in order.

        The parsed report's own warnings stay first because they came
        from the agent/report payload; callback warnings are runner
        observability notes appended after.
        """
        merged = list(base)
        for warning in self._warnings:
            if warning not in merged:
                merged.append(warning)
        return merged


@dataclass
class SubagentRunCallbacks:
    """
    Safe engine callbacks for one subagent process.

    Engine callbacks are part of the launch boundary: they must update
    live PID/progress state when possible, but a persistence failure in
    those callbacks must not crash the still-running engine process.
    This wrapper owns that best-effort behavior and exposes whether the
    engine has been observed as started.
    """

    task: TaskRecord
    """Task the subagent is running under."""

    base: Path
    """Artifact directory allocated for this subagent."""

    ref: Subagent
    """Canonical subagent descriptor persisted on the task record."""

    prompt: str
    """Original prompt sent to the engine."""

    sessions: SubagentPidRecorder
    """Session manager used to persist PID updates."""

    progress_writer: ProgressSnapshotWriter
    """Writer used to persist live progress snapshots."""

    warnings: CallbackWarnings = field(default_factory=CallbackWarnings)
    """Collector for non-fatal callback bookkeeping warnings."""

    engine_started: bool = False
    """True after the adapter reports the engine process has started."""

    def on_started(self, pid: int) -> None:
        """
        Record the engine PID once the adapter reports process start.
        """
        self.engine_started = True
        try:
            self.sessions.record_subagent_pid(self.task, self.ref, pid)
        except Exception as exc:  # callback failures must not crash the runner
            self.warnings.record_failure(self.ref, "start", exc)

    def on_update(self, execution: CLIExecutionResult) -> None:
        """
        Persist one live progress snapshot from an engine update.
        """
        if execution.pid is not None:
            self.engine_started = True
        try:
            self.progress_writer.write_session_progress(
                self.task,
                self.base,
                self.ref,
                self.prompt,
                execution,
            )
        except Exception as exc:  # progress persistence must not crash the runner
            self.warnings.record_failure(self.ref, "progress", exc)
