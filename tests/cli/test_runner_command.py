from pathlib import Path

from typer.testing import CliRunner

from litehive.cli.app import app as root_app
from litehive.config.workspace import ensure_workspace


def test_run_rejects_dry_run_flag(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    result = CliRunner().invoke(
        root_app,
        ["run", "--dry-run", "--workspace", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "No such option: --dry-run" in result.output
