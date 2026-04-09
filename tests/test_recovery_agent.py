from pathlib import Path

import yaml

from litehive.config import ensure_workspace
from litehive.models import RuntimeStageState, SubagentRef
from litehive.pipeline import TaskExecutionRunner
from litehive.pipeline import _attempt_commit_recovery
from litehive.subagents import SubagentResult
from litehive.tasks import create_task, get_task, save_task
from litehive.tasks.crud import save_task_runtime
from litehive.tasks.persistence import load_state, save_state
from litehive.tasks.queue_ops import dequeue_next_task_selection, set_active_task
from litehive.pipeline.recovery import recover_stale_runner_state
from tests.workspace_helpers import _init_git_repo


def _load_recovery_report(root: Path, task_id: str, slug: str) -> dict[str, object]:
    report_path = root / ".litehive" / "tasks" / f"{task_id}-{slug}" / "recovery" / "recovery-001.yaml"
    return yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}


def test_recover_stale_runner_state_writes_structured_recovery_report(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover stale runner")
    task.status = "in_progress"
    task.pipeline_status = "testing"
    task.runtime.execution_status = "running"
    task.runtime.current_stage = RuntimeStageState(
        step="testing",
        status="running",
        started_at="2026-04-01T00:00:00+00:00",
        updated_at="2026-04-01T00:00:00+00:00",
    )
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)
    set_active_task(tmp_path, task.id)

    mutated = recover_stale_runner_state(tmp_path)

    assert mutated is True
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    report = _load_recovery_report(tmp_path, refreshed.id, refreshed.slug)
    assert report["trigger"] == "stale_runner_recovery"
    assert report["runnable_state"] == "runnable"
    assert report["actions"][0]["action"] == "clear_stale_active_state"
    assert report["actions"][1]["action"] == "requeue_stage"


def test_dequeue_flagged_task_writes_recovery_report_and_requeues(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Flagged recovery")
    task.status = "flagged"
    task.pipeline_status = "implementing"
    task.runtime.execution_status = "flagged"
    task.runtime.last_outcome.kind = "flagged"
    task.runtime.last_outcome.reason_code = "stage_exception"
    task.runtime.last_outcome.reason = "implementing failed"
    save_task(tmp_path, task)
    save_task_runtime(tmp_path, task)

    state = load_state(tmp_path)
    state.queue = [task.id]
    save_state(tmp_path, state)

    selection = dequeue_next_task_selection(tmp_path)

    assert selection.task is not None
    refreshed = get_task(tmp_path, task.id)
    assert refreshed is not None
    report = _load_recovery_report(tmp_path, refreshed.id, refreshed.slug)
    assert report["trigger"] == "flagged_task"
    assert report["runnable_state"] == "runnable"
    assert report["actions"][0]["action"] == "requeue_stage"
    assert refreshed.status == "in_progress"
    assert refreshed.pipeline_status == "implementing"


class _FakeRecoveryManager:
    def run(self, task, *, role, engine_name, prompt, model=None, max_turns=None, resume_session_id=None):  # type: ignore[no-untyped-def]
        return SubagentResult(
            ref=SubagentRef(
                id="SA-9999",
                role=role,
                engine=engine_name,
                status="completed",
                path="subagents/SA-9999-recovery",
                sandboxed=False,
                sandbox_summary="host",
            ),
            execution=None,
            transcript="recovered commit flow",
            exit_code=0,
            failure=None,
        )


def test_attempt_commit_recovery_writes_structured_report(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Recover commit failure")
    task.pipeline_status = "commit_to_git"
    task.status = "flagged"
    save_task(tmp_path, task)

    sha = _attempt_commit_recovery(
        tmp_path,
        tmp_path,
        task,
        "merge conflict",
        subagents=_FakeRecoveryManager(),  # type: ignore[arg-type]
        config=None,
    )

    assert sha is not None
    report = _load_recovery_report(tmp_path, task.id, task.slug)
    assert report["trigger"] == "commit_to_git_failure"
    assert report["runnable_state"] == "runnable"
    assert report["actions"][0]["action"] == "recover_commit_to_git"
    assert report["recovery_subagent_id"] == "SA-9999"


def test_runner_records_blocked_recovery_report_when_preflight_cannot_be_repaired(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Blocked late preflight")

    def executor(task, step):  # type: ignore[no-untyped-def]
        from litehive.models import StageReport

        if step == "commit_to_git":
            task.status = "done"
            task.pipeline_status = "done"
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    result = TaskExecutionRunner(tmp_path, executor).run(task)

    # With the new commit flow, the custom executor returns pass for all
    # stages including commit_to_git, so the task completes successfully.
    # The late-stage preflight that previously blocked was removed.
    assert result.final_status == "done"
