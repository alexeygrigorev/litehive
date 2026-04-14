import io
from pathlib import Path
import shutil

import yaml

from litehive.attention import list_attention, record_attention, resolve_attention
from litehive.cli.attention import cmd_attention_list, cmd_attention_resolve
from litehive.config.model import LitehiveConfig
from litehive.config.paths import worktree_root
from litehive.daemon.execution import run_daemon_loop
from litehive.main import _fast_status
from litehive.domain.task import WorkspaceState
from litehive.state.records import create_task, save_task
from litehive.tasks.persistence import load_state, save_state, set_pool_stop_reason
from tests.workspace_helpers import ensure_workspace


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

    resolve_code = cmd_attention_resolve(
        type("Args", (), {"workspace": tmp_path, "attention_id": item.id})()
    )
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

    current_state = yaml.safe_load((tmp_path / ".litehive" / "state.yaml").read_text(encoding="utf-8")) or {}
    current_state["active_task_id"] = flagged.id
    current_state["pool_stop_reason"] = "human_checkpoint_before_commit"
    (tmp_path / ".litehive" / "state.yaml").write_text(
        yaml.safe_dump(current_state, sort_keys=False),
        encoding="utf-8",
    )
    save_state(tmp_path, WorkspaceState(**current_state))

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


def test_pool_stops_before_running_tasks_when_attention_gate_enabled(
    tmp_path: Path, monkeypatch
) -> None:
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


def test_pool_halts_immediately_when_local_main_diverges_from_origin(
    tmp_path: Path, monkeypatch
) -> None:
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
    assert "!!! ATTENTION REQUIRED !!! Local main has diverged from origin/main. Halting pool: diverged_from_origin" in output
    assert "git fetch origin main" in output
    assert "git log --oneline --left-right main...origin/main" in output
    assert load_state(tmp_path).pool_stop_reason == "diverged_from_origin"
