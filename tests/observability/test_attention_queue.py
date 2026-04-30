import io
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from litehive.attention import (
    AttentionStoreError,
    list_attention,
    record_attention,
    resolve_attention,
    waiting_for_you_lines,
)
from litehive.cli.attention import cmd_attention_list, cmd_attention_resolve
from litehive.config.model import LitehiveConfig
from litehive.config.paths import workspace_path
from litehive.config.registry import workspace_registry_path
from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import run_daemon_loop
from litehive.db.schema import connect_workspace_db
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.lifecycle.types import PipelineMode
from litehive.main import fast_status
from litehive.sandbox.git_wrapper import main as git_wrapper_main
from litehive.state.records import create_task, save_task
from litehive.state.persist import load_state, save_state, set_pool_stop_reason


def _attention_item_paths(root: Path) -> list[Path]:
    return sorted((root / ".litehive" / "attention").glob("*.yaml"))


def _attention_payloads(root: Path) -> list[dict]:
    with connect_workspace_db(root) as connection:
        rows = connection.execute("SELECT id, payload FROM attention ORDER BY id ASC").fetchall()
    payloads = []
    for row in rows:
        payload = json.loads(row["payload"])
        payload["id"] = row["id"]
        payloads.append(payload)
    return payloads


def test_attention_list_and_resolve_persist_items(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    item = record_attention(
        tmp_path,
        kind="destructive_git_denied",
        title="Destructive git command was blocked",
        reason="`git push --force origin main` was rejected.",
        suggested_action="Use a safe git command and then run `litehive attention resolve <id>`.",
        dedupe_key="destructive_git_denied:test",
    )

    exit_code = cmd_attention_list(type("Args", (), {"workspace": tmp_path})())
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "pending_attention: 1" in output
    assert f"attention: {item.id}" in output
    assert "Destructive git command was blocked" in output
    assert "suggested_action:" in output
    assert _attention_item_paths(tmp_path) == []
    persisted = _attention_payloads(tmp_path)[0]
    assert persisted["id"] == item.id
    assert persisted["title"] == "Destructive git command was blocked"
    assert persisted["reason"] == "`git push --force origin main` was rejected."
    assert persisted["suggested_action"] == "Use a safe git command and then run `litehive attention resolve <id>`."
    assert persisted["status"] == "pending"

    resolve_code = cmd_attention_resolve(type("Args", (), {"workspace": tmp_path, "attention_id": item.id})())
    resolved_output = capsys.readouterr().out

    assert resolve_code == 0
    assert f"resolved_attention: {item.id}" in resolved_output
    assert list_attention(tmp_path) == []
    resolved_payload = _attention_payloads(tmp_path)[0]
    assert resolved_payload["status"] == "resolved"
    assert resolved_payload["resolution"] == "resolved by operator"
    assert resolved_payload["resolved_at"] is not None


def test_status_shows_attention_count_and_waiting_actions(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    record_attention(
        tmp_path,
        kind="destructive_git_denied",
        title="Destructive git command was blocked",
        reason="`git reset --hard origin/main` was rejected.",
        suggested_action="Use a safe git command and then run `litehive attention resolve <id>`.",
        dedupe_key="destructive_git_denied:status",
    )

    exit_code = fast_status(["--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "attention_items: 1" in output
    assert "waiting for you:" in output
    assert "Destructive git command was blocked" in output


def test_waiting_for_you_lines_reports_database_unavailable(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.setattr(
        "litehive.attention.list_attention",
        lambda root, reconcile=True, auto_resolve=True: (_ for _ in ()).throw(sqlite3.DatabaseError("boom")),
    )

    lines = waiting_for_you_lines(tmp_path)

    assert len(lines) == 1
    assert lines[0].startswith("attention_items: unavailable (DatabaseError: boom)")


def test_corrupt_attention_row_reports_unavailable_instead_of_empty_queue(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    with connect_workspace_db(tmp_path) as connection:
        connection.execute(
            """
            INSERT INTO attention (task_id, created_at, kind, payload)
            VALUES (?, ?, ?, ?)
            """,
            (None, "2026-04-30T00:00:00Z", "destructive_git_denied", "{not-json"),
        )
        connection.commit()

    with pytest.raises(AttentionStoreError, match="corrupt attention state"):
        list_attention(tmp_path, reconcile=False)

    lines = waiting_for_you_lines(tmp_path)

    assert len(lines) == 1
    assert lines[0].startswith("attention_items: unavailable (AttentionStoreError: corrupt attention state")
    assert "row 1" in lines[0]


def test_status_reconciles_detectable_attention_items_without_prior_listing(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)
    primary = create_task(tmp_path, title="Primary task")
    duplicate_dir = tmp_path / ".litehive" / "tasks" / f"{primary.id}-duplicate-copy"
    duplicate_dir.mkdir(parents=True)

    exit_code = fast_status(["--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "attention_items: 1" in output
    assert "waiting for you:" in output
    assert f"Duplicate task id detected for {primary.id}" in output
    assert _attention_item_paths(tmp_path) == []
    assert len(_attention_payloads(tmp_path)) == 1


def test_git_wrapper_block_records_attention_db_item(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)

    exit_code = git_wrapper_main(
        ["push", "--force", "origin", "main"],
        real_git_path="/usr/bin/git",
        workspace_root=str(tmp_path),
    )
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "blocked destructive git command" in err
    items = list_attention(tmp_path, reconcile=False)
    assert len(items) == 1
    item = items[0]
    assert item.kind == "destructive_git_denied"
    assert item.reason == "`git push --force origin main` was rejected: push with force or mirror is not allowed"
    payload = _attention_payloads(tmp_path)[0]
    assert payload["kind"] == "destructive_git_denied"
    assert payload["metadata"]["command"] == "git push --force origin main"


def test_attention_store_reads_legacy_database_rows(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    with connect_workspace_db(tmp_path) as connection:
        connection.execute(
            """
            INSERT INTO attention (task_id, created_at, kind, payload)
            VALUES (?, ?, ?, ?)
            """,
            (
                None,
                "2026-04-23T10:00:00Z",
                "destructive_git_denied",
                '{"kind": "destructive_git_denied", "title": "Legacy item", "reason": "legacy reason", '
                '"suggested_action": "legacy action", "dedupe_key": "legacy-key", "status": "pending"}',
            ),
        )
        connection.commit()

    items = list_attention(tmp_path, reconcile=False)

    assert len(items) == 1
    assert items[0].title == "Legacy item"
    assert _attention_item_paths(tmp_path) == []
    migrated = _attention_payloads(tmp_path)[0]
    assert migrated["title"] == "Legacy item"
    assert migrated["dedupe_key"] == "legacy-key"


def test_detectable_attention_items_reconcile_and_auto_clear(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)

    primary = create_task(tmp_path, title="Primary task")
    duplicate_dir = tmp_path / ".litehive" / "tasks" / f"{primary.id}-duplicate-copy"
    duplicate_dir.mkdir(parents=True)

    flagged = create_task(tmp_path, title="Flag me")
    flagged.status = "flagged"
    flagged.pipeline_status = "implementing"
    flagged.flag_reason = "needs operator review"
    save_task(tmp_path, flagged)

    merge_failed = create_task(tmp_path, title="Recover merge")
    merge_failed.status = "flagged"
    merge_failed.pipeline_status = "flagged"
    merge_failed.flag_reason = "merge_failed"
    save_task(tmp_path, merge_failed)

    stale = create_task(tmp_path, title="Stale worktree")
    stale.status = "done"
    stale.pipeline_status = "done"
    missing_worktree = workspace_path(tmp_path, "worktrees") / "stale-task"
    stale.runtime.pipeline.git.worktree_path = str(missing_worktree)
    save_task(tmp_path, stale)
    missing_worktree.mkdir(parents=True)

    current_state = load_state(tmp_path)
    current_state.active_task_id = flagged.id
    current_state.pool_stop_reason = "human_checkpoint_before_commit"
    save_state(tmp_path, current_state)

    monkeypatch.setattr(
        "litehive.daemon.execution.check_origin_divergence",
        lambda workspace: "local main and origin/main have diverged",
    )

    items = list_attention(tmp_path)
    kinds = {item.kind for item in items}

    assert "duplicate_task_id" in kinds
    assert "flagged_task" in kinds
    assert "merge_failed_task" in kinds
    assert "stale_worktree" in kinds
    assert "origin_divergence" in kinds
    assert "human_checkpoint_before_commit" in kinds
    flagged_item = next(item for item in items if item.kind == "flagged_task")
    assert flagged_item.suggested_action == (
        f"Run `litehive task debug {flagged.id}` and then `litehive queue promote {flagged.id}` when it is ready to continue."
    )
    merge_item = next(item for item in items if item.kind == "merge_failed_task")
    assert merge_item.suggested_action == (
        f"Run `litehive task debug {merge_failed.id} --worktree` and then `litehive queue requeue {merge_failed.id}`."
    )
    checkpoint_item = next(item for item in items if item.kind == "human_checkpoint_before_commit")
    assert checkpoint_item.suggested_action == (
        f"Run `litehive task debug {flagged.id} --worktree` to inspect the task, then continue with `litehive run` when you are ready to commit."
    )

    shutil.rmtree(duplicate_dir)
    flagged.status = "queued"
    flagged.pipeline_status = "backlog"
    flagged.flag_reason = None
    save_task(tmp_path, flagged)
    merge_failed.status = "done"
    merge_failed.pipeline_status = "done"
    save_task(tmp_path, merge_failed)
    missing_worktree.rmdir()
    set_pool_stop_reason(tmp_path, None)
    monkeypatch.setattr("litehive.daemon.execution.check_origin_divergence", lambda workspace: None)

    remaining = list_attention(tmp_path)
    assert remaining == []


def test_stale_worktree_metadata_auto_resolves_when_path_already_clear(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Already cleared worktree metadata")
    task.status = "done"
    task.pipeline_status = "done"
    task.runtime.pipeline.git.worktree_path = None
    save_task(tmp_path, task)

    record_attention(
        tmp_path,
        task_id=task.id,
        kind="stale_worktree_metadata",
        title=f"Deferred worktree metadata clearing for {task.id}",
        reason="Worktree removed but task metadata clearing deferred due to active runner lock",
        suggested_action="Wait for runner to finish, then run attention reconciliation",
        dedupe_key=f"stale_worktree_metadata:{task.id}",
    )

    assert list_attention(tmp_path) == []
    payloads = _attention_payloads(tmp_path)
    assert len(payloads) == 1
    assert payloads[0]["status"] == "resolved"
    assert payloads[0]["resolution"] == "auto-resolved: worktree metadata already clear"


def test_attention_ignores_non_task_directories_under_worktrees(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    (workspace_path(tmp_path, "worktrees") / "heru").mkdir(parents=True)

    assert list_attention(tmp_path) == []


def test_merge_failed_attention_refreshes_to_recovery_follow_up_when_commit_recovery_crashes(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Commit crash mislabeled as merge")
    task.status = "flagged"
    task.pipeline_status = "flagged"
    task.flag_reason = "merge_failed"
    save_task(tmp_path, task)

    persistence = SqlitePersistence(tmp_path)
    state = persistence.initialize(task.id, pipeline_mode=PipelineMode.FULL)
    state.stage = "failed"
    state.failed_reason = "recovery_crashed"
    state.active_recovery_trigger = RecoveryTrigger(
        origin_stage="commit",
        trigger_event_kind=TriggerEventKind.CRASH,
        failure_fingerprint=FailureFingerprint(
            fingerprint="GitError:merge aborted",
            classification="GitError",
        ),
        message="merge aborted",
    )
    persistence.save(state)

    record_attention(
        tmp_path,
        kind="merge_failed_task",
        task_id=task.id,
        title=f"Task {task.id} needs merge recovery",
        reason="old reason",
        suggested_action="old action",
        dedupe_key=f"merge_failed_task:{task.id}",
    )

    items = list_attention(tmp_path)

    merge_item = next(item for item in items if item.kind == "merge_failed_task")
    assert merge_item.title == f"Task {task.id} needs recovery follow-up"
    assert merge_item.reason == "Recovery crashed while handling commit-stage failure; operator follow-up is required."
    assert merge_item.suggested_action == (
        f"Run `litehive task debug {task.id} --worktree` and then `litehive queue requeue {task.id}`."
    )


def test_duplicate_id_detection_uses_task_directory_ids_without_task_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    primary = create_task(tmp_path, title="Primary task")
    duplicate_dir = tmp_path / ".litehive" / "tasks" / f"{primary.id}-broken-copy"
    duplicate_dir.mkdir(parents=True)

    items = list_attention(tmp_path)

    assert any(item.kind == "duplicate_task_id" for item in items)


def test_operator_resolve_suppresses_detectable_attention_until_condition_clears(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    primary = create_task(tmp_path, title="Primary task")
    duplicate_dir = tmp_path / ".litehive" / "tasks" / f"{primary.id}-duplicate-copy"
    duplicate_dir.mkdir(parents=True)

    first_items = list_attention(tmp_path)
    first = next(item for item in first_items if item.kind == "duplicate_task_id")
    resolved = resolve_attention(tmp_path, first.id or 0)

    assert resolved is not None
    assert resolved.status == "resolved"
    assert list_attention(tmp_path) == []

    shutil.rmtree(duplicate_dir)
    assert list_attention(tmp_path) == []

    duplicate_dir.mkdir(parents=True)

    second_items = list_attention(tmp_path)
    second = next(item for item in second_items if item.kind == "duplicate_task_id")

    assert second.id != first.id


def test_pool_stops_before_running_tasks_when_attention_gate_enabled(tmp_path: Path, monkeypatch) -> None:
    config = LitehiveConfig(pool_stop_on_attention=True)
    ensure_workspace(tmp_path, config)
    create_task(tmp_path, title="Queued work")
    record_attention(
        tmp_path,
        kind="destructive_git_denied",
        title="Destructive git command was blocked",
        reason="`git push --force origin main` was rejected.",
        suggested_action="Use a safe git command and then run `litehive attention resolve <id>`.",
        dedupe_key="destructive_git_denied:pool",
    )

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        if any(arg == "run" for arg in command[2:]):
            raise AssertionError("pool should not start a task while attention is pending")
        return 0

    monkeypatch.setattr("litehive.daemon.execution.load_config", lambda workspace: config)
    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")
    output = stream.getvalue()

    assert exit_code == 0
    assert any("repair" in command for command in calls)
    assert "Pool stopped: attention_required" in output


def test_pool_resumes_after_attention_items_are_resolved(tmp_path: Path, monkeypatch) -> None:
    config = LitehiveConfig(pool_stop_on_attention=True)
    ensure_workspace(tmp_path, config)
    create_task(tmp_path, title="Queued work")
    item = record_attention(
        tmp_path,
        kind="destructive_git_denied",
        title="Destructive git command was blocked",
        reason="`git push --force origin main` was rejected.",
        suggested_action="Use a safe git command and then run `litehive attention resolve <id>`.",
        dedupe_key="destructive_git_denied:resume",
    )

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        if any(arg == "run" for arg in command[2:]):
            state = load_state(tmp_path)
            state.queue = []
            save_state(tmp_path, state)
        return 0

    monkeypatch.setattr("litehive.daemon.execution.load_config", lambda workspace: config)
    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)

    first_stream = io.StringIO()
    first_exit_code = run_daemon_loop(tmp_path, output_stream=first_stream, session_dir=tmp_path / "logs-first")

    resolved = resolve_attention(tmp_path, item.id or 0)
    second_stream = io.StringIO()
    second_exit_code = run_daemon_loop(tmp_path, output_stream=second_stream, session_dir=tmp_path / "logs-second")

    assert first_exit_code == 0
    assert "Pool stopped: attention_required" in first_stream.getvalue()
    assert resolved is not None
    assert second_exit_code == 0
    assert "Pool already stopped: attention_required" not in second_stream.getvalue()
    assert any("run" in command for command in calls)


def test_pool_halts_immediately_when_local_main_diverges_from_origin(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued work")

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        raise AssertionError("daemon should halt before repair or run when main diverges")

    monkeypatch.setattr(
        "litehive.daemon.execution.check_origin_divergence",
        lambda workspace: (
            "local main (12345678) and origin/main (abcdef12) have diverged. "
            "Manual reconciliation required: run `git fetch origin main`, inspect "
            "`git log --oneline --left-right main...origin/main`, then rebase, reset, or merge "
            "before restarting the pool."
        ),
    )
    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")
    output = stream.getvalue()

    assert exit_code == 0
    assert calls == []
    assert (
        "!!! ATTENTION REQUIRED !!! Local main has diverged from origin/main. Halting pool: diverged_from_origin"
        in output
    )
    assert "git fetch origin main" in output
    assert "git log --oneline --left-right main...origin/main" in output
    assert load_state(tmp_path).pool_stop_reason == "diverged_from_origin"


def test_daemon_loop_rebuilds_corrupt_or_missing_global_registry_without_exiting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    ensure_workspace(tmp_path)

    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.maybe_run_workspace_backup", lambda *args, **kwargs: None)

    for scenario in ("corrupt", "missing"):
        create_task(tmp_path, title=f"Queued work {scenario}")
        registry_path = workspace_registry_path()
        if scenario == "corrupt":
            registry_path.write_bytes(b"not a sqlite database")
        else:
            for path in (registry_path, registry_path.with_name(registry_path.name + "-wal")):
                path.unlink(missing_ok=True)
            registry_path.with_name(registry_path.name + "-shm").unlink(missing_ok=True)

        calls: list[tuple[str, ...]] = []

        def fake_run_logged_subprocess(command, **kwargs):
            calls.append(tuple(command))
            if any(arg == "run" for arg in command[2:]):
                state = load_state(tmp_path)
                state.queue = []
                save_state(tmp_path, state)
            return 0

        monkeypatch.setattr("litehive.daemon.execution.run_logged_subprocess", fake_run_logged_subprocess)

        stream = io.StringIO()
        exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / f"logs-{scenario}")
        output = stream.getvalue()

        assert exit_code == 0
        assert any("repair" in command for command in calls)
        assert any("run" in command for command in calls)
        assert "== iteration 1 ==" in output

        with sqlite3.connect(workspace_registry_path()) as connection:
            rows = connection.execute(
                "SELECT root FROM workspace_registry ORDER BY registered_at DESC, root DESC"
            ).fetchall()
        assert rows == [(str(tmp_path.resolve()),)]
