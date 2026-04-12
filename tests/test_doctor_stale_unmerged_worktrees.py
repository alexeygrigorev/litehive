from __future__ import annotations

from pathlib import Path

from litehive.models import WorkspaceState
from tests.workspace_helpers import (
    _cmd_doctor,
    argparse,
    create_task,
    ensure_workspace,
    load_state,
    save_state,
    save_task,
)


def test_doctor_prunes_stale_unmerged_worktrees_entries(tmp_path: Path, capsys) -> None:
    ensure_workspace(tmp_path)

    done_task = create_task(tmp_path, title="Done task", auto_commit=False)
    done_task.status = "done"
    done_task.pipeline_status = "done"
    save_task(tmp_path, done_task)

    queued_task = create_task(tmp_path, title="Queued task", auto_commit=False)

    existing_worktree = tmp_path / "existing-unmerged-worktree"
    existing_worktree.mkdir(parents=True, exist_ok=True)

    save_state(
        tmp_path,
        WorkspaceState(
            unmerged_worktrees=[
                {
                    "task_id": done_task.id,
                    "worktree_path": "existing-unmerged-worktree",
                },
                {
                    "task_id": queued_task.id,
                    "worktree_path": ".litehive/worktrees/missing-worktree",
                },
            ]
        ),
    )

    exit_code = _cmd_doctor(argparse.Namespace(workspace=tmp_path, fix=False))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "doctor_cleanup: stale_unmerged_worktrees_removed=2" in output
    assert "doctor: clean" in output
    assert "stale_worktree" not in output
    assert load_state(tmp_path).unmerged_worktrees == []

    second_exit_code = _cmd_doctor(argparse.Namespace(workspace=tmp_path, fix=False))
    second_output = capsys.readouterr().out

    assert second_exit_code == 0
    assert "doctor_cleanup: stale_unmerged_worktrees_removed=" not in second_output
    assert "doctor: clean" in second_output
