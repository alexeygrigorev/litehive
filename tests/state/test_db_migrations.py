from pathlib import Path
import sqlite3

import pytest
from typer.testing import CliRunner

from litehive.agents.session_store import load_subagent_report, save_subagent_artifacts
from litehive.cli.app import app
from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.state.records import create_task
from litehive.db import schema as schema_module
from litehive.db.schema import (
    Migration,
    MigrationApplyError,
    apply_pending_migrations,
    available_migrations,
    connect_workspace_db,
)


def _write_cache_tool(cache_target: Path) -> None:
    cache_target.parent.mkdir(parents=True, exist_ok=True)
    cache_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cache_target.chmod(0o755)


def _create_broken_venv_binary(checkout_root: Path, binary_name: str, cache_root: Path) -> None:
    bin_dir = checkout_root / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cache_target = cache_root / f"{binary_name}-tool"
    _write_cache_tool(cache_target)
    (bin_dir / binary_name).symlink_to(cache_target)
    cache_target.unlink()


def test_embedded_initial_migration_is_discoverable() -> None:
    migrations = available_migrations()

    names = [migration.name for migration in migrations]
    assert names == ["0001_initial.sql", "0002_task_audit_log.sql"]
    assert migrations[0].version == 1
    assert "CREATE TABLE IF NOT EXISTS pool_state" in migrations[0].sql
    assert "CREATE TABLE IF NOT EXISTS pipeline_transitions" in migrations[0].sql
    assert "CREATE TABLE IF NOT EXISTS pipeline_task_state" in migrations[0].sql
    assert migrations[1].version == 2
    assert "CREATE TABLE IF NOT EXISTS task_audit_log" in migrations[1].sql


def test_db_status_and_dry_run_report_pending_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    staged = (
        *available_migrations(),
        Migration(version=3, name="0003_add_marker.sql", sql="CREATE TABLE marker (id INTEGER PRIMARY KEY);"),
    )
    monkeypatch.setattr("litehive.db.schema.available_migrations", lambda: staged)

    status = CliRunner().invoke(app, ["db", "status", "--workspace", str(tmp_path)])
    dry_run = CliRunner().invoke(app, ["db", "migrate", "--dry-run", "--workspace", str(tmp_path)])

    assert status.exit_code == 0, status.output
    assert "schema_version: 2" in status.output
    assert "pending_migrations: 1" in status.output
    assert "pending: 0003_add_marker.sql" in status.output

    assert dry_run.exit_code == 0, dry_run.output
    assert "dry_run: yes" in dry_run.output
    assert "would_apply: 0003_add_marker.sql" in dry_run.output

    with sqlite3.connect(workspace_path(tmp_path, "data.db")) as connection:
        marker = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'marker'"
        ).fetchone()
    assert marker is None


def test_apply_pending_migrations_rolls_back_failed_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    staged = (
        *available_migrations(),
        Migration(
            version=3,
            name="0003_broken.sql",
            sql=("CREATE TABLE broken_marker (id INTEGER PRIMARY KEY);INSERT INTO missing_table(value) VALUES (1);"),
        ),
    )
    monkeypatch.setattr("litehive.db.schema.available_migrations", lambda: staged)

    with pytest.raises(MigrationApplyError):
        apply_pending_migrations(tmp_path)

    with sqlite3.connect(workspace_path(tmp_path, "data.db")) as connection:
        applied_versions = [
            row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]
        broken_marker = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'broken_marker'"
        ).fetchone()

    assert applied_versions == [1, 2]
    assert broken_marker is None


def test_daemon_run_applies_pending_migrations_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    staged = (
        *available_migrations(),
        Migration(
            version=3,
            name="0003_daemon_marker.sql",
            sql="CREATE TABLE daemon_marker (id INTEGER PRIMARY KEY);",
        ),
    )
    monkeypatch.setattr("litehive.db.schema.available_migrations", lambda: staged)
    monkeypatch.setattr("litehive.cli.runner.start_background_daemon", lambda root: 4321)

    result = CliRunner().invoke(
        app,
        ["daemon", "run", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )
    output = result.output

    assert result.return_value == 0
    assert "daemon_status: running" in output
    with sqlite3.connect(workspace_path(tmp_path, "data.db")) as connection:
        applied_versions = [
            row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]
        daemon_marker = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daemon_marker'"
        ).fetchone()
    assert applied_versions == [1, 2, 3]
    assert daemon_marker is not None


def test_daemon_run_reports_broken_worktree_venv_before_start(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued work")
    broken_worktree = workspace_path(tmp_path, "worktrees") / "T-0001-demo"
    _create_broken_venv_binary(broken_worktree, "ruff", tmp_path / "fake-home" / ".cache" / "uv")

    result = CliRunner().invoke(
        app,
        ["daemon", "run", "--workspace", str(tmp_path)],
        standalone_mode=False,
    )

    assert result.return_value == 1
    assert "daemon run failed: broken virtualenv entrypoints blocked pool start:" in result.output
    assert "binary=ruff" in result.output
    assert f"venv={broken_worktree / '.venv'} checkout={broken_worktree}" in result.output
    assert "uv venv --clear .venv && uv sync --extra dev" in result.output


def test_legacy_workspace_db_is_rebuilt_without_task_yaml_rescan(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Keep me")
    db_path = workspace_path(tmp_path, "data.db")
    db_path.unlink()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            [
                (1, "0001_initial.sql", "2026-04-15T00:00:00Z"),
                (2, "0002_pipeline_journal.sql", "2026-04-15T00:00:01Z"),
                (3, "0003_pipeline_task_state.sql", "2026-04-15T00:00:02Z"),
            ],
        )
        connection.execute(
            "CREATE TABLE task_state (task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO task_state (task_id, payload, updated_at) VALUES (?, ?, ?)",
            ("T-9999", "{}", "2026-04-15T00:00:03Z"),
        )
        connection.commit()

    apply_pending_migrations(tmp_path)
    ensure_workspace(tmp_path)

    with sqlite3.connect(db_path) as connection:
        applied_versions = [
            row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]
        rows = connection.execute("SELECT task_id FROM task_state ORDER BY task_id").fetchall()
        queue_row = connection.execute("SELECT payload FROM queue WHERE workspace_key = 'workspace'").fetchone()

    assert applied_versions == [1, 2]
    assert rows == []
    assert queue_row is not None
    assert queue_row[0] == "[]"


def test_connect_workspace_db_rebuilds_replaced_cached_db(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    db_path = workspace_path(tmp_path, "data.db")

    save_subagent_artifacts(
        tmp_path,
        "T-0001",
        "SA-0001",
        session={"status": "running"},
    )

    db_path.unlink()
    with sqlite3.connect(db_path):
        pass

    save_subagent_artifacts(
        tmp_path,
        "T-0001",
        "SA-0001",
        report={"summary": "recovered"},
    )

    report = load_subagent_report(tmp_path, "T-0001", "SA-0001")
    assert report["summary"] == "recovered"

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }

    assert "subagent_sessions" in tables


def test_connect_workspace_db_cache_ignores_normal_db_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_workspace(tmp_path)
    schema_module.MIGRATED_DB_PATHS.clear()

    apply_calls = 0
    real_apply = schema_module.apply_pending_migrations

    def counting_apply(root: Path, *, dry_run: bool = False):
        nonlocal apply_calls
        apply_calls += 1
        return real_apply(root, dry_run=dry_run)

    monkeypatch.setattr(schema_module, "apply_pending_migrations", counting_apply)

    with connect_workspace_db(tmp_path) as connection:
        connection.execute(
            "UPDATE queue SET payload = ?, updated_at = ? WHERE workspace_key = ?",
            ('["T-0001"]', "2026-04-23T00:00:00Z", "workspace"),
        )
        connection.commit()

    with connect_workspace_db(tmp_path) as connection:
        payload = connection.execute(
            "SELECT payload FROM queue WHERE workspace_key = ?",
            ("workspace",),
        ).fetchone()[0]

    assert payload == '["T-0001"]'
    assert apply_calls == 1
