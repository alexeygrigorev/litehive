from pathlib import Path

from heru.types import SubagentRef

from litehive.config.workspace import ensure_workspace
from litehive.domain.reports import StageReport
from litehive.domain.runtime import RuntimeInterruptionState
from litehive.state.records import create_task, require_task, save_task
from litehive.tasks.runtime import (
    mark_stage_finished,
    mark_stage_started,
    mark_subagent_finished,
    mark_subagent_started,
    mark_task_run_started,
)


def _subagent_ref() -> SubagentRef:
    return SubagentRef(
        id="SA-0001",
        role="swe",
        engine="codex",
        status="running",
        path="subagents/SA-0001-swe",
    )


def test_mark_task_run_started_resets_stage_and_active_subagent(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Reset runtime")

    mark_stage_started(tmp_path, task, "implementing")
    task = require_task(tmp_path, task.id)
    mark_subagent_started(tmp_path, task, _subagent_ref())

    task = require_task(tmp_path, task.id)
    task.runtime.interruption = RuntimeInterruptionState(source="runner", reason="stale state")
    save_task(tmp_path, task)

    mark_task_run_started(tmp_path, task)

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.runtime.execution_status == "running"
    assert refreshed.runtime.current_stage.stage is None
    assert refreshed.runtime.current_stage.status == "idle"
    assert refreshed.runtime.active_subagent is None
    assert refreshed.runtime.interruption is None


def test_mark_stage_finished_uses_shared_idle_and_completed_stage_shapes(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish stage")

    mark_stage_started(tmp_path, task, "implementing")
    task = require_task(tmp_path, task.id)
    report = StageReport(
        task_id=task.id,
        pipeline_state="implementing",
        verdict="pass",
        summary="implemented the change",
    )

    mark_stage_finished(tmp_path, task, report)

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.runtime.current_stage.stage is None
    assert refreshed.runtime.current_stage.status == "idle"
    assert "last" + "_stage" not in refreshed.runtime.model_dump()["pipeline"]


def test_mark_subagent_finished_clears_active_subagent_without_completed_runtime_copy(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    task = create_task(tmp_path, title="Finish subagent")
    ref = _subagent_ref()

    mark_subagent_started(tmp_path, task, ref)

    mark_subagent_finished(tmp_path, task, ref, "SUMMARY: partial output", exit_code=0)

    refreshed = require_task(tmp_path, task.id)
    assert refreshed.runtime.active_subagent is None
    assert "last" + "_subagent" not in refreshed.runtime.model_dump()["execution"]
