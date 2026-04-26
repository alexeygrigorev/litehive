import logging
from pathlib import Path

import pytest

import litehive.state.persist as workflow_module
from litehive.config.workspace import ensure_workspace
from litehive.state.persist import load_state
from litehive.state.records import create_task, get_task, save_task


def test_save_task_rolls_back_task_record_when_runtime_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litehive.state.store import RuntimeStore

    ensure_workspace(tmp_path)

    task = create_task(tmp_path, title="Atomic save", auto_commit=False)
    task.status = "flagged"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "flagged"

    def fail_runtime_transaction(self, *, task_intents=None, task_states=None, workspace_state=None):
        del task_intents, task_states, workspace_state
        raise OSError("runtime write failed")

    monkeypatch.setattr(RuntimeStore, "save_runtime_transaction", fail_runtime_transaction)

    with pytest.raises(OSError, match="runtime write failed"):
        save_task(tmp_path, task)

    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    assert refreshed.status == "queued"
    assert refreshed.pipeline_status == "backlog"
    assert refreshed.runtime.execution_status == "idle"


def test_workspace_transition_writes_preserve_task_added_after_state_snapshot(
    tmp_path: Path,
) -> None:
    ensure_workspace(tmp_path)
    active = create_task(tmp_path, title="Active task")
    queued = create_task(tmp_path, title="Queued task")
    stale_state = load_state(tmp_path)
    create_task(tmp_path, title="Added later")
    active.status = "done"
    active.pipeline_status = "done"
    stale_state.active_task_id = None
    stale_state.queue = [queued.id]

    merged_state = workflow_module.merged_state_for_runner_owned_write(
        tmp_path,
        state=stale_state,
        protected_task_ids=[active.id],
    )

    assert merged_state.queue == ["T-0002", "T-0003"]
    assert merged_state.next_task_number == 3


def test_create_task_surfaces_cleanup_failure_when_rollback_removal_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ensure_workspace(tmp_path)

    def fail_write_atomic_files_and_then(writes, callback) -> None:
        del writes, callback
        raise OSError("intent write failed")

    def fail_rmtree(path: Path) -> None:
        raise OSError(f"permission denied for {path}")

    monkeypatch.setattr("litehive.state.records.write_atomic_files_and_then", fail_write_atomic_files_and_then)
    monkeypatch.setattr("litehive.fs_cleanup.shutil.rmtree", fail_rmtree)

    with caplog.at_level(logging.INFO, logger="litehive.state.records"):
        with pytest.raises(
            ExceptionGroup, match="failed to persist created tasks and roll back created task directories"
        ) as excinfo:
            create_task(tmp_path, title="Cleanup failure surfaces")

    assert any("intent write failed" in str(error) for error in excinfo.value.exceptions)
    assert any("failed to delete created task directory" in str(error) for error in excinfo.value.exceptions)
    assert "Deleting created task directory" in caplog.text
