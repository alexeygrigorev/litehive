from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from typer.testing import CliRunner

from litehive.cli import app
from litehive.cli.backup import _cmd_backup_create, _cmd_backup_list, _cmd_backup_restore
from litehive.config import workspace_backups_dir, workspace_database_path
from litehive.models import RunnerStatusState
from litehive.storage import create_workspace_backup, list_workspace_backups

from tests.workspace_helpers import Path, argparse, ensure_workspace, pytest


def _seed_workspace_db(root: Path, values: list[str]) -> None:
    with sqlite3.connect(workspace_database_path(root)) as connection:
        connection.execute("DROP TABLE IF EXISTS backup_test")
        connection.execute("CREATE TABLE backup_test (value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO backup_test(value) VALUES (?)",
            [(value,) for value in values],
        )
        connection.commit()


def _read_workspace_db_values(root: Path) -> list[str]:
    with sqlite3.connect(workspace_database_path(root)) as connection:
        rows = connection.execute("SELECT value FROM backup_test ORDER BY rowid").fetchall()
    return [row[0] for row in rows]


def test_create_backup_and_restore_backup_cli(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _seed_workspace_db(tmp_path, ["before"])
    backup = create_workspace_backup(tmp_path, when=datetime(2026, 4, 11, 2, tzinfo=UTC))
    _seed_workspace_db(tmp_path, ["after"])

    result = CliRunner().invoke(
        app,
        ["backup", "restore", backup.timestamp, "--workspace", str(tmp_path)],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "Restore backup 2026-04-11T02" in result.output
    assert "restored: 2026-04-11T02" in result.output
    assert _read_workspace_db_values(tmp_path) == ["before"]


def test_backup_list_command_reports_timestamp_and_size(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    _seed_workspace_db(tmp_path, ["one"])
    create_workspace_backup(tmp_path, when=datetime(2026, 4, 11, 3, tzinfo=UTC))

    exit_code = _cmd_backup_list(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "backups: 1" in output
    assert "timestamp: 2026-04-11T03" in output
    assert "size_bytes: " in output
    assert str(workspace_backups_dir(tmp_path) / "data-2026-04-11T03.db.gz") in output


def test_backup_create_command_reports_created_archive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ensure_workspace(tmp_path)
    _seed_workspace_db(tmp_path, ["one"])

    exit_code = _cmd_backup_create(argparse.Namespace(workspace=tmp_path))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "timestamp: " in output
    assert "path: " in output
    assert "size_bytes: " in output
    assert list_workspace_backups(tmp_path)


def test_restore_command_refuses_when_daemon_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    monkeypatch.setattr("litehive.cli.backup.get_workspace_daemon", lambda root: {"pid": 123})

    exit_code = _cmd_backup_restore(
        argparse.Namespace(workspace=tmp_path, timestamp="2026-04-11T02", yes=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "backup restore failed: workspace daemon is running" in output


def test_restore_command_refuses_when_runner_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)

    monkeypatch.setattr("litehive.cli.backup.get_workspace_daemon", lambda root: None)
    monkeypatch.setattr(
        "litehive.cli.backup.runner_status",
        lambda root: RunnerStatusState(status="running", pid=321),
    )

    exit_code = _cmd_backup_restore(
        argparse.Namespace(workspace=tmp_path, timestamp="2026-04-11T02", yes=True)
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "backup restore failed: workspace runner is active" in output


def test_backup_rotation_keeps_seven_daily_and_four_weekly(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _seed_workspace_db(tmp_path, ["one"])
    timestamps = [
        "2026-04-11T02",
        "2026-04-10T02",
        "2026-04-09T02",
        "2026-04-08T02",
        "2026-04-07T02",
        "2026-04-06T02",
        "2026-04-05T02",
        "2026-03-29T02",
        "2026-03-22T02",
        "2026-03-15T02",
        "2026-03-08T02",
        "2026-03-01T02",
        "2026-02-22T02",
    ]

    for timestamp in reversed(timestamps):
        create_workspace_backup(tmp_path, when=datetime.strptime(timestamp, "%Y-%m-%dT%H").replace(tzinfo=UTC))

    backups = list_workspace_backups(tmp_path)

    assert [backup.timestamp for backup in backups] == [
        "2026-04-11T02",
        "2026-04-10T02",
        "2026-04-09T02",
        "2026-04-08T02",
        "2026-04-07T02",
        "2026-04-06T02",
        "2026-04-05T02",
        "2026-03-29T02",
        "2026-03-22T02",
        "2026-03-15T02",
        "2026-03-08T02",
    ]


def test_daemon_loop_creates_scheduled_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import litehive.daemon as daemon_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace(workspace)
    (workspace / ".litehive" / "state.yaml").write_text(
        "active_task_id: null\nqueue: []\npool_stop_reason: null\n",
        encoding="utf-8",
    )

    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "if [[ \"${1:-}\" == \"run\" && \"${2:-}\" == \"litehive\" && \"${3:-}\" == \"repair\" ]]; then",
                "  echo \"repaired: no\"",
                "  exit 0",
                "fi",
                "echo \"unexpected uv invocation: $*\" >&2",
                "exit 1",
            ]
        ),
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    monkeypatch.setattr(
        "litehive.daemon._execution._default_command_prefix",
        lambda: [str(fake_uv), "run", "litehive"],
    )

    exit_code = daemon_module.run_daemon_loop(workspace, output_stream=None)

    assert exit_code == 0
    backups = list_workspace_backups(workspace)
    assert len(backups) == 1
    assert backups[0].path.parent == workspace_backups_dir(workspace)
