from __future__ import annotations

import argparse
import sqlite3

from typer.testing import CliRunner

from litehive.cli import app
from litehive.cli.daemon import _cmd_daemon_run
from litehive.config import workspace_database_path
from litehive.db.schema import Migration, MigrationApplyError, apply_pending_migrations, available_migrations

from tests.workspace_helpers import Path, ensure_workspace, pytest


def test_embedded_initial_migration_is_discoverable() -> None:
    migrations = available_migrations()

    names = [migration.name for migration in migrations]
    assert names == [
        "0001_initial.sql",
        "0002_pipeline_journal.sql",
        "0003_pipeline_task_state.sql",
    ]
    assert migrations[0].version == 1
    assert migrations[1].version == 2
    assert migrations[2].version == 3
    assert "CREATE TABLE IF NOT EXISTS pool_state" in migrations[0].sql
    assert "CREATE TABLE IF NOT EXISTS pipeline_transitions" in migrations[1].sql
    assert "CREATE TABLE IF NOT EXISTS pipeline_task_state" in migrations[2].sql


def test_db_status_and_dry_run_report_pending_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    staged = (
        *available_migrations(),
        Migration(version=4, name="0004_add_marker.sql", sql="CREATE TABLE marker (id INTEGER PRIMARY KEY);"),
    )
    monkeypatch.setattr("litehive.db.schema.available_migrations", lambda: staged)

    status = CliRunner().invoke(app, ["db", "status", "--workspace", str(tmp_path)])
    dry_run = CliRunner().invoke(app, ["db", "migrate", "--dry-run", "--workspace", str(tmp_path)])

    assert status.exit_code == 0, status.output
    assert "schema_version: 3" in status.output
    assert "pending_migrations: 1" in status.output
    assert "pending: 0004_add_marker.sql" in status.output

    assert dry_run.exit_code == 0, dry_run.output
    assert "dry_run: yes" in dry_run.output
    assert "would_apply: 0004_add_marker.sql" in dry_run.output

    with sqlite3.connect(workspace_database_path(tmp_path)) as connection:
        marker = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'marker'"
        ).fetchone()
    assert marker is None


def test_apply_pending_migrations_rolls_back_failed_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace(tmp_path)
    staged = (
        *available_migrations(),
        Migration(
            version=4,
            name="0004_broken.sql",
            sql=(
                "CREATE TABLE broken_marker (id INTEGER PRIMARY KEY);"
                "INSERT INTO missing_table(value) VALUES (1);"
            ),
        ),
    )
    monkeypatch.setattr("litehive.db.schema.available_migrations", lambda: staged)

    with pytest.raises(MigrationApplyError):
        apply_pending_migrations(tmp_path)

    with sqlite3.connect(workspace_database_path(tmp_path)) as connection:
        applied_versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        broken_marker = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'broken_marker'"
        ).fetchone()

    assert applied_versions == [1, 2, 3]
    assert broken_marker is None


def test_daemon_run_applies_pending_migrations_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    staged = (
        *available_migrations(),
        Migration(
            version=4,
            name="0004_daemon_marker.sql",
            sql="CREATE TABLE daemon_marker (id INTEGER PRIMARY KEY);",
        ),
    )
    monkeypatch.setattr("litehive.db.schema.available_migrations", lambda: staged)
    monkeypatch.setattr("litehive.cli.daemon.start_background_daemon", lambda root: 4321)

    exit_code = _cmd_daemon_run(argparse.Namespace(workspace=tmp_path, foreground=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "daemon_status: running" in output
    with sqlite3.connect(workspace_database_path(tmp_path)) as connection:
        applied_versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        daemon_marker = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daemon_marker'"
        ).fetchone()
    assert applied_versions == [1, 2, 3, 4]
    assert daemon_marker is not None
