import json
from pathlib import Path
import sqlite3

import pytest
from typer.testing import CliRunner

from litehive.agents.session_store import SubagentArtifactPayload, load_subagent_report, save_subagent_artifacts
from litehive.cli.app import app
from litehive.config.paths import workspace_path
from litehive.config.workspace import ensure_workspace
from litehive.domain.task import TaskStateRecord, WorkspaceState
from litehive.recovery.detection import TaskLaunchFailure
from litehive.state.records import create_task, get_task, list_tasks
from litehive.state.store import runtime_store
from litehive.tasks.queue import peek_next_task
from litehive.workspace import Workspace
from litehive.db import schema as schema_module
from litehive.db.schema import (
    Migration,
    MigrationApplyError,
    apply_pending_migrations,
    available_migrations,
    connect_workspace_db,
)
from litehive.state.rebuild_safety import RebuildSafetyError
from litehive.tasks.event_log import task_event_log_path


def _install_workspace_db_schema(root: Path, *, through_version: int) -> None:
    db_path = workspace_path(root, "data.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for migration in available_migrations():
            if migration.version > through_version:
                continue
            connection.executescript(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, f"2026-04-15T00:00:0{migration.version}Z"),
            )
        connection.commit()


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


def _migration_versions() -> list[int]:
    return [migration.version for migration in available_migrations()]


def _latest_migration_version() -> int:
    return max(_migration_versions())


def _next_migration_version() -> int:
    return _latest_migration_version() + 1


def test_embedded_initial_migration_is_discoverable() -> None:
    migrations = available_migrations()

    names = [migration.name for migration in migrations]
    assert names == [
        "0001_initial.sql",
        "0002_task_audit_log.sql",
        "0003_stage_reports_pipeline_state.sql",
        "0004_recovery_reports.sql",
        "0005_task_intent.sql",
        "0006_runtime_settings.sql",
        "0007_task_metadata_and_process_state.sql",
        "0008_remove_pipeline_session_turn_metric.sql",
        "0009_attention_log.sql",
        "0010_subagent_id_counters.sql",
    ]
    assert migrations[0].version == 1
    assert "CREATE TABLE IF NOT EXISTS pool_state" in migrations[0].sql
    assert "CREATE TABLE IF NOT EXISTS pipeline_transitions" in migrations[0].sql
    assert "CREATE TABLE IF NOT EXISTS pipeline_task_state" in migrations[0].sql
    assert migrations[1].version == 2
    assert "CREATE TABLE IF NOT EXISTS task_audit_log" in migrations[1].sql
    assert migrations[2].version == 3
    assert "RENAME COLUMN stage TO pipeline_state" in migrations[2].sql
    assert migrations[3].version == 4
    assert "CREATE TABLE IF NOT EXISTS recovery_reports" in migrations[3].sql
    assert migrations[4].version == 5
    assert "CREATE TABLE IF NOT EXISTS task_intent" in migrations[4].sql
    assert migrations[5].version == 6
    assert "CREATE TABLE IF NOT EXISTS runtime_settings" in migrations[5].sql
    assert migrations[6].version == 7
    assert "ALTER TABLE task_intent ADD COLUMN slug" in migrations[6].sql
    assert "CREATE TABLE IF NOT EXISTS runtime_process_state" in migrations[6].sql
    assert migrations[7].version == 8
    assert "pipeline_sessions_new" in migrations[7].sql
    assert "DROP TABLE pipeline_sessions" in migrations[7].sql
    assert migrations[8].version == 9
    assert "CREATE TABLE IF NOT EXISTS attention_log" in migrations[8].sql
    assert migrations[9].version == 10
    assert "CREATE TABLE IF NOT EXISTS subagent_id_counters" in migrations[9].sql


def test_connect_workspace_db_closes_connection_on_context_exit(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    with connect_workspace_db(tmp_path) as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_db_status_and_dry_run_report_pending_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    next_version = _next_migration_version()
    next_name = f"{next_version:04d}_add_marker.sql"
    staged = (
        *available_migrations(),
        Migration(version=next_version, name=next_name, sql="CREATE TABLE marker (id INTEGER PRIMARY KEY);"),
    )
    monkeypatch.setattr("litehive.db.schema.available_migrations", lambda: staged)

    status = CliRunner().invoke(app, ["db", "status", "--workspace", str(tmp_path)])
    dry_run = CliRunner().invoke(app, ["db", "migrate", "--dry-run", "--workspace", str(tmp_path)])

    assert status.exit_code == 0, status.output
    assert f"schema_version: {_latest_migration_version()}" in status.output
    assert "pending_migrations: 1" in status.output
    assert f"pending: {next_name}" in status.output

    assert dry_run.exit_code == 0, dry_run.output
    assert "dry_run: yes" in dry_run.output
    assert f"would_apply: {next_name}" in dry_run.output

    with sqlite3.connect(workspace_path(tmp_path, "data.db")) as connection:
        marker = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'marker'"
        ).fetchone()
    assert marker is None


def test_apply_pending_migrations_rolls_back_failed_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_workspace(tmp_path)
    next_version = _next_migration_version()
    staged = (
        *available_migrations(),
        Migration(
            version=next_version,
            name=f"{next_version:04d}_broken.sql",
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

    assert applied_versions == _migration_versions()
    assert broken_marker is None


def test_migration_0005_does_not_import_deprecated_task_yaml(tmp_path: Path) -> None:
    litehive_dir = tmp_path / ".litehive"
    task_dir = litehive_dir / "tasks" / "T-0001-existing-task"
    task_dir.mkdir(parents=True)
    (litehive_dir / "config.yaml").write_text("default_engine: codex\n", encoding="utf-8")

    task_yaml = task_dir / "task.yaml"
    task_yaml.write_text("id: T-0001\ntitle: Existing task\n", encoding="utf-8")

    _install_workspace_db_schema(tmp_path, through_version=4)
    updated_at = "2026-04-15T00:00:10Z"
    workspace_state_payload = WorkspaceState(queue=["T-0001"], next_task_number=1).model_dump(mode="json")
    queue_payload = json.dumps(workspace_state_payload.pop("queue"), sort_keys=True)
    task_state_payload = TaskStateRecord().model_dump(mode="json")
    task_state_payload["updated_at"] = updated_at
    with sqlite3.connect(workspace_path(tmp_path, "data.db")) as connection:
        connection.execute(
            "INSERT INTO pool_state (workspace_key, payload, updated_at) VALUES (?, ?, ?)",
            ("workspace", json.dumps(workspace_state_payload, sort_keys=True), updated_at),
        )
        connection.execute(
            "INSERT INTO queue (workspace_key, payload, updated_at) VALUES (?, ?, ?)",
            ("workspace", queue_payload, updated_at),
        )
        connection.execute(
            "INSERT INTO task_state (task_id, payload, updated_at) VALUES (?, ?, ?)",
            ("T-0001", json.dumps(task_state_payload, sort_keys=True), updated_at),
        )
        connection.commit()

    apply_pending_migrations(tmp_path)
    runtime_store(tmp_path).bootstrap()

    with connect_workspace_db(tmp_path) as connection:
        intent_rows = connection.execute("SELECT task_id, payload FROM task_intent ORDER BY task_id").fetchall()
        queue_row = connection.execute("SELECT payload FROM queue WHERE workspace_key = ?", ("workspace",)).fetchone()
        applied_versions = [
            row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]

    loaded = get_task(tmp_path, "T-0001")
    listed = list_tasks(tmp_path, strict=False)
    with pytest.raises(TaskLaunchFailure, match="missing from SQLite task_intent"):
        peek_next_task(Workspace.from_path(tmp_path))

    assert applied_versions == _migration_versions()
    assert intent_rows == []
    assert queue_row is not None
    assert json.loads(queue_row["payload"]) == ["T-0001"]
    assert loaded is None
    assert listed == []
    assert task_yaml.exists()
    assert sorted(litehive_dir.rglob("*.yaml")) == [
        litehive_dir / "config.yaml",
        task_yaml,
    ]


def test_daemon_run_applies_pending_migrations_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_workspace(tmp_path)
    next_version = _next_migration_version()
    next_name = f"{next_version:04d}_daemon_marker.sql"
    staged = (
        *available_migrations(),
        Migration(
            version=next_version,
            name=next_name,
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
    assert applied_versions == [*_migration_versions(), next_version]
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


def test_legacy_workspace_db_rebuild_replays_task_event_log_without_task_yaml_rescan(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Keep me")
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
            (task.id, "{}", "2026-04-15T00:00:03Z"),
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

    assert applied_versions == _migration_versions()
    assert rows == [(task.id,)]
    assert queue_row is not None
    assert json.loads(queue_row[0]) == [task.id]


def test_migration_rebuild_refuses_to_drop_tasks_missing_from_event_log(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Historical task before event log")
    task_event_log_path(Workspace.from_path(tmp_path)).unlink()
    db_path = workspace_path(tmp_path, "data.db")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET name = ? WHERE version = ?",
            ("0002_legacy_name.sql", 2),
        )
        connection.commit()

    with pytest.raises(RebuildSafetyError, match="refusing migration-triggered database rebuild"):
        apply_pending_migrations(tmp_path)

    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT task_id FROM task_state WHERE task_id = ?", (task.id,)).fetchone()

    assert row == (task.id,)


def test_connect_workspace_db_rebuilds_replaced_cached_db(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    db_path = workspace_path(tmp_path, "data.db")

    save_subagent_artifacts(
        Workspace.from_path(tmp_path),
        "T-0001",
        "SA-0001",
        session=SubagentArtifactPayload({"status": "running"}),
    )

    db_path.unlink()
    with sqlite3.connect(db_path):
        pass

    save_subagent_artifacts(
        Workspace.from_path(tmp_path),
        "T-0001",
        "SA-0001",
        report=SubagentArtifactPayload({"summary": "recovered"}),
    )

    report = load_subagent_report(Workspace.from_path(tmp_path), "T-0001", "SA-0001")
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
