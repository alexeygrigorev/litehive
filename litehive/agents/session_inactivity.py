"""Inactivity timeout policy and watchdog for subagent engine sessions."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import time
from typing import TYPE_CHECKING

from heru.base import CLIExecutionResult

from litehive.domain.agent import SubagentInactivityTimeout

if TYPE_CHECKING:
    from litehive.config.model import LitehiveConfig

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
class SubagentInactivityMonitor:
    """
    Watch stdout activity and terminate stale subagent engine processes.
    """

    policy: SubagentInactivityTimeoutPolicy

    def live_timeout_seconds(self, engine_name: str) -> float:
        """
        Return the stdout idle budget the watchdog enforces for an engine.
        """
        return self.policy.live_timeout_seconds(engine_name)

    def completed_timeout(self, execution: CLIExecutionResult) -> SubagentInactivityTimeout | None:
        """
        Detect a watchdog-killed completed run from its stderr marker.
        """
        return self.policy.completed_timeout(execution)

    def check_stdout_inactivity(
        self,
        base: Path,
        engine_name: str,
        execution: CLIExecutionResult,
    ) -> None:
        """
        Kill an engine that has stopped producing stdout, then raise the timeout.
        """
        if execution.pid is None:
            return
        stdout_path = base / "stdout.txt"
        if not stdout_path.exists():
            return
        limit_seconds = self.live_timeout_seconds(engine_name)
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
        """
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
