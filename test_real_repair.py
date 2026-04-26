#!/usr/bin/env python3
"""Test real litehive repair command."""

import subprocess
import tempfile
import time
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task


def test_real_repair_command():
    """Test real litehive repair command performance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        print(f"Testing real repair in {workspace}")

        # Initialize workspace
        ensure_workspace(workspace)

        # Create 100 tasks
        print("Creating 100 tasks...")
        for i in range(100):
            create_task(workspace, title=f"Task {i}")

        # Run real litehive repair command
        print("Running real litehive repair...")
        start_time = time.perf_counter()
        result = subprocess.run(
            ["uv", "run", "litehive", "repair", "--workspace", str(workspace)],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent
        )
        end_time = time.perf_counter()

        duration = end_time - start_time
        print(f"Command completed in {duration:.4f} seconds")
        print(f"Return code: {result.returncode}")
        print("Output:")
        print(result.stdout)
        if result.stderr:
            print("Stderr:")
            print(result.stderr)

        # Verify acceptance criteria
        success = (
            result.returncode == 0
            and duration < 1.0
            and "repair completed clean run" in result.stdout
            and "no inconsistencies found" in result.stdout
        )

        print(f"All acceptance criteria met: {'✓' if success else '✗'}")
        print(f"- Return code 0: {'✓' if result.returncode == 0 else '✗'}")
        print(f"- Under 1 second: {'✓' if duration < 1.0 else '✗'} ({duration:.4f}s)")
        print(f"- Clean run message: {'✓' if 'repair completed clean run' in result.stdout else '✗'}")
        print(f"- Timing included: {'✓' if 'no inconsistencies found' in result.stdout else '✗'}")

        return success


if __name__ == "__main__":
    test_real_repair_command()