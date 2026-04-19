import io
from pathlib import Path
import shutil
import sqlite3

import yaml

from litehive.attention import list_attention, record_attention, resolve_attention, waiting_for_you_lines
from litehive.cli.attention import cmd_attention_list, cmd_attention_resolve
from litehive.config.model import LitehiveConfig
from litehive.config.paths import litehive_database_path, worktree_root
from litehive.config.workspace import ensure_workspace
from litehive.daemon.execution import run_daemon_loop
from litehive.domain.recovery import FailureFingerprint, RecoveryTrigger, TriggerEventKind
from litehive.lifecycle.persistence import SqlitePersistence
from litehive.lifecycle.types import PipelineMode
from litehive.main import _fast_status
from litehive.state.records import create_task, save_task
from litehive.state.persist import load_state, save_state, set_pool_stop_reason


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

    resolve_code = cmd_attention_resolve(type("Args", (), {"workspace": tmp_path, "attention_id": item.id})())
    resolved_output = capsys.readouterr().out

    assert resolve_code == 0
    assert f"resolved_attention: {item.id}" in resolved_output
    assert list_attention(tmp_path) == []


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

    exit_code = _fast_status(["--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "attention_items: 1" in output
    assert "waiting for you:" in output
    assert "Destructive git command was blocked" in output


def test_waiting_for_you_lines_reports_database_unavailable(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    monkeypatch.setattr(
        "litehive.attention.list_attention",
        lambda root, reconcile=True: (_ for _ in ()).throw(sqlite3.DatabaseError("boom")),
    )

    assert waiting_for_you_lines(tmp_path) == ["attention_items: unavailable"]


def test_detectable_attention_items_reconcile_and_auto_clear(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)

    primary = create_task(tmp_path, title="Primary task")
    duplicate_dir = tmp_path / ".litehive" / "tasks" / f"{primary.id}-duplicate-copy"
    duplicate_dir.mkdir(parents=True)
    (duplicate_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": primary.id,
                "slug": "duplicate-copy",
                "title": "Duplicate copy",
                "mode": "implementation",
                "pipeline_mode": "full",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    flagged = create_task(tmp_path, title="Flag me")
    flagged.status = "flagged"
    flagged.pipeline_status = "implementing"
    flagged.flag_reason = "needs operator review"
    save_task(tmp_path, flagged)

    merge_failed = create_task(tmp_path, title="Recover merge")
    merge_failed.status = "merge_failed"
    merge_failed.pipeline_status = "merge_failed"
    save_task(tmp_path, merge_failed)

    stale = create_task(tmp_path, title="Stale worktree")
    stale.status = "done"
    stale.pipeline_status = "done"
    missing_worktree = worktree_root(tmp_path) / "stale-task"
    stale.runtime.git.worktree_path = str(missing_worktree)
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
        f"Run `litehive task debug {merge_failed.id} --worktree` and then `litehive recover {merge_failed.id}`."
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


def test_merge_failed_attention_refreshes_to_recovery_follow_up_when_commit_recovery_crashes(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Commit crash mislabeled as merge")
    task.status = "merge_failed"
    task.pipeline_status = "merge_failed"
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
        f"Run `litehive task debug {task.id} --worktree` and then `litehive recover {task.id}`."
    )


def test_duplicate_id_detection_ignores_non_mapping_task_yaml(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    primary = create_task(tmp_path, title="Primary task")
    duplicate_dir = tmp_path / ".litehive" / "tasks" / f"{primary.id}-broken-copy"
    duplicate_dir.mkdir(parents=True)
    (duplicate_dir / "task.yaml").write_text("- not-a-task-mapping\n", encoding="utf-8")

    items = list_attention(tmp_path)

    assert all(item.kind != "duplicate_task_id" for item in items)


def test_operator_resolve_suppresses_detectable_attention_until_condition_clears(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)

    primary = create_task(tmp_path, title="Primary task")
    duplicate_dir = tmp_path / ".litehive" / "tasks" / f"{primary.id}-duplicate-copy"
    duplicate_dir.mkdir(parents=True)
    (duplicate_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": primary.id,
                "slug": "duplicate-copy",
                "title": "Duplicate copy",
                "mode": "implementation",
                "pipeline_mode": "full",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    first_items = list_attention(tmp_path)
    first = next(item for item in first_items if item.kind == "duplicate_task_id")
    resolved = resolve_attention(tmp_path, first.id or 0)

    assert resolved is not None
    assert resolved.status == "resolved"
    assert list_attention(tmp_path) == []

    shutil.rmtree(duplicate_dir)
    assert list_attention(tmp_path) == []

    duplicate_dir.mkdir(parents=True)
    (duplicate_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": primary.id,
                "slug": "duplicate-copy",
                "title": "Duplicate copy",
                "mode": "implementation",
                "pipeline_mode": "full",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

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
    monkeypatch.setattr("litehive.daemon.execution._run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution._maybe_run_workspace_backup", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")
    output = stream.getvalue()

    assert exit_code == 0
    assert any("repair" in command for command in calls)
    assert "Pool stopped: attention_required" in output


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
    monkeypatch.setattr("litehive.daemon.execution._run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution._maybe_run_workspace_backup", lambda *args, **kwargs: None)

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


def test_daemon_loop_rebuilds_corrupt_global_registry_without_exiting(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued work")

    from litehive.config import registry as registry_mod

    registry_mod._close_all_registry_connections()
    litehive_database_path().write_bytes(b"not a sqlite database")

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        if any(arg == "run" for arg in command[2:]):
            state = load_state(tmp_path)
            state.queue = []
            save_state(tmp_path, state)
        return 0

    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution._run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution._maybe_run_workspace_backup", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")
    output = stream.getvalue()

    assert exit_code == 0
    assert any("repair" in command for command in calls)
    assert any("run" in command for command in calls)
    assert "== iteration 1 ==" in output

    with sqlite3.connect(litehive_database_path()) as connection:
        paths = [str(row[0]) for row in connection.execute("SELECT path FROM workspaces").fetchall()]
    assert paths == [str(tmp_path.resolve())]


def test_pool_refuses_to_start_when_worktree_venv_is_broken(tmp_path: Path, monkeypatch) -> None:
    ensure_workspace(tmp_path)
    create_task(tmp_path, title="Queued work")
    broken_worktree = worktree_root(tmp_path) / "T-0001-demo"
    _create_broken_venv_binary(broken_worktree, "ruff", tmp_path / "fake-home" / ".cache" / "uv")

    calls: list[tuple[str, ...]] = []

    def fake_run_logged_subprocess(command, **kwargs):
        calls.append(tuple(command))
        raise AssertionError("daemon should stop before repair or run subprocesses")

    monkeypatch.setattr("litehive.daemon.execution.register_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution.unregister_daemon", lambda *args, **kwargs: None)
    monkeypatch.setattr("litehive.daemon.execution._run_logged_subprocess", fake_run_logged_subprocess)
    monkeypatch.setattr("litehive.daemon.execution._maybe_run_workspace_backup", lambda *args, **kwargs: None)

    stream = io.StringIO()
    exit_code = run_daemon_loop(tmp_path, output_stream=stream, session_dir=tmp_path / "logs")
    output = stream.getvalue()

    assert exit_code == 1
    assert calls == []
    assert "broken virtualenv entrypoints blocked pool start:" in output
    assert f"venv={broken_worktree / '.venv'} checkout={broken_worktree}" in output
    assert "binary=ruff" in output
    assert "uv venv --clear .venv && uv sync --extra dev" in output
