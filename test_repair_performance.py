#!/usr/bin/env python3
"""Test repair performance on a clean queue."""

import tempfile
import time
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.recovery.workspace_repair import repair_workspace_state
from litehive.state.records import create_task


def test_repair_timing():
    """Test repair performance on clean 100-task queue."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        print(f"Testing repair performance in {workspace}")

        # Initialize workspace
        ensure_workspace(workspace)

        # Create 100 tasks
        print("Creating 100 tasks...")
        for i in range(100):
            create_task(workspace, title=f"Task {i}")

        # Time the repair operation
        print("Running repair...")
        start_time = time.perf_counter()
        summary = repair_workspace_state(workspace)
        end_time = time.perf_counter()

        duration = end_time - start_time
        print(f"Repair completed in {duration:.4f} seconds")
        print(f"Repair summary: mutated={summary.mutated}, stale_runner_recovered={summary.stale_runner_recovered}")

        # Check acceptance criteria
        success = duration < 1.0
        print(f"Under 1 second: {'✓' if success else '✗'} ({duration:.4f}s)")

        return success, duration


if __name__ == "__main__":
    test_repair_timing()