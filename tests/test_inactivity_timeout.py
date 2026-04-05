"""Tests that the inactivity timeout in run_live fires even when data flows."""

import sys
import textwrap
from pathlib import Path

import pytest

from litehive.external_cli import ExternalCLIAdapter, AdapterCapabilities, CLIInvocation


class _StubAdapter(ExternalCLIAdapter):
    """Minimal adapter that runs a given Python snippet."""

    LIVE_UPDATE_INTERVAL_SECONDS = 0.05  # speed up tests

    def __init__(self, script: str) -> None:
        super().__init__(
            name="stub",
            binary=sys.executable,
            capabilities=AdapterCapabilities(available=True),
        )
        self._script = script

    def build_command(self, prompt, cwd, model=None, *, max_turns=None, resume_session_id=None):
        return [sys.executable, "-c", self._script]


class TestInactivityTimeout:
    def test_timeout_fires_when_stderr_writes_continuously(self, tmp_path: Path):
        """Subprocess writes to stderr every 0.1s but nothing to stdout.

        The inactivity timeout (based on stdout only) should fire.
        """
        script = textwrap.dedent("""\
            import sys, time
            while True:
                sys.stderr.write("noise\\n")
                sys.stderr.flush()
                time.sleep(0.1)
        """)
        adapter = _StubAdapter(script)
        result = adapter.run_live(
            prompt="unused",
            cwd=tmp_path,
            inactivity_timeout_seconds=0.5,
        )
        assert "inactivity" in result.stderr.lower()
        # Process was killed, so exit code should be non-zero or we broke out
        assert result.exit_code != 0 or "killed" in result.stderr.lower()

    def test_timeout_does_not_fire_when_stdout_writes(self, tmp_path: Path):
        """Subprocess writes to stdout regularly and exits on its own.

        The timeout should NOT fire because stdout resets the timer.
        """
        script = textwrap.dedent("""\
            import sys, time
            for i in range(5):
                sys.stdout.write(f"line {i}\\n")
                sys.stdout.flush()
                time.sleep(0.1)
        """)
        adapter = _StubAdapter(script)
        result = adapter.run_live(
            prompt="unused",
            cwd=tmp_path,
            inactivity_timeout_seconds=1.0,
        )
        assert "inactivity" not in result.stderr.lower()
        assert result.exit_code == 0

    def test_timeout_fires_after_stdout_stops(self, tmp_path: Path):
        """Subprocess writes to stdout briefly, then goes silent.

        The timeout should fire after stdout stops even though the process
        is still alive.
        """
        script = textwrap.dedent("""\
            import sys, time
            sys.stdout.write("hello\\n")
            sys.stdout.flush()
            time.sleep(10)
        """)
        adapter = _StubAdapter(script)
        result = adapter.run_live(
            prompt="unused",
            cwd=tmp_path,
            inactivity_timeout_seconds=0.5,
        )
        assert "hello" in result.stdout
        assert "inactivity" in result.stderr.lower()
