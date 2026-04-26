#!/usr/bin/env python3
"""Test repair CLI output."""

import tempfile
from pathlib import Path
from typer.testing import CliRunner

from litehive.cli.app import app
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task


def test_repair_cli_output():
    """Test repair CLI output on clean 100-task queue."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        print(f"Testing repair CLI in {workspace}")

        # Initialize workspace
        ensure_workspace(workspace)

        # Create 100 tasks
        print("Creating 100 tasks...")
        for i in range(100):
            create_task(workspace, title=f"Task {i}")

        # Run CLI repair
        print("Running CLI repair...")
        runner = CliRunner()
        result = runner.invoke(app, ["repair", "--workspace", str(workspace)], standalone_mode=False)

        print(f"Return code: {result.return_value}")
        print("Output:")
        print(result.output)

        return result


if __name__ == "__main__":
    test_repair_cli_output()