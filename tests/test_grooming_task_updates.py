import pytest
from pathlib import Path
from litehive.models import TaskRecord, StageReport
from litehive.tasks import (
    create_task,
    get_task,
    apply_task_updates_from_report,
)
from litehive.config import ensure_workspace


def test_apply_task_updates_from_structured_report(tmp_path):
    root = ensure_workspace(tmp_path)
    task = create_task(root, title="New Task", goal="Original goal")

    report = StageReport(
        task_id=task.id,
        step="grooming",
        verdict="pass",
        summary="Grooming done with structured updates.",
        task_update={
            "goal": "Refined goal",
            "pm_complexity": "moderate",
            "planned_effort": "m",
            "constraints": ["Constraint 1"],
            "plan": ["Step 1", "Step 2"],
            "priority": "high",
        },
    )

    apply_task_updates_from_report(root, task, report)

    updated = get_task(root, task.id)
    assert updated.goal == "Refined goal"
    assert updated.pm_complexity == "moderate"
    assert updated.planned_effort == "m"
    assert updated.constraints == ["Constraint 1"]
    assert updated.plan == ["Step 1", "Step 2"]
    assert updated.priority == "high"
    assert task.goal == "Refined goal"


def test_apply_task_updates_from_text_fallback(tmp_path):
    root = ensure_workspace(tmp_path)
    task = create_task(root, title="New Task", goal="Original goal")

    report = StageReport(
        task_id=task.id,
        step="grooming",
        verdict="pass",
        summary="Grooming done with text fallback.",
        feedback="""
ACCEPTANCE_CRITERIA:
- AC 1
CONSTRAINTS:
- C 1
PLAN:
- P 1
PM_COMPLEXITY: moderate
PLANNED_EFFORT: s
""",
    )

    apply_task_updates_from_report(root, task, report)

    updated = get_task(root, task.id)
    assert updated.acceptance_criteria == ["AC 1"]
    assert updated.constraints == ["C 1"]
    assert updated.plan == ["P 1"]
    assert updated.pm_complexity == "moderate"
    assert updated.planned_effort == "s"


def test_apply_task_updates_from_yaml_block(tmp_path):
    root = ensure_workspace(tmp_path)
    task = create_task(root, title="New Task", goal="Original goal")

    from litehive.engines.base import parse_stage_report_text
    import textwrap

    transcript = textwrap.dedent("""
        SUMMARY: Grooming done.
        TASK_UPDATE:
          goal: YAML updated goal
          pm_complexity: complex
          auto_commit: false
        VERDICT: PASS
    """).strip()
    report = parse_stage_report_text(
        task_id=task.id, step="grooming", transcript=transcript, subagent_status="completed"
    )
    assert report.task_update == {
        "goal": "YAML updated goal",
        "pm_complexity": "complex",
        "auto_commit": False,
    }

    apply_task_updates_from_report(root, task, report)

    updated = get_task(root, task.id)
    assert updated.goal == "YAML updated goal"
    assert updated.pm_complexity == "complex"
    assert updated.git.auto_commit is False


def test_apply_task_updates_with_outcome_duplicate_closes_task(tmp_path):
    root = ensure_workspace(tmp_path)
    task = create_task(root, title="Dupe task", goal="Check duplicate closure")
    state = __import__("litehive.tasks", fromlist=["load_state"]).load_state(root)
    state.queue.append(task.id)
    __import__("litehive.tasks", fromlist=["save_state"]).save_state(root, state)

    report = StageReport(
        task_id=task.id,
        step="grooming",
        verdict="pass",
        summary="This is a duplicate of T-0001.",
        task_update={
            "outcome": "duplicate",
            "outcome_reason": "Duplicate of T-0001",
        },
    )

    apply_task_updates_from_report(root, task, report)

    updated = get_task(root, task.id)
    assert updated.status == "duplicate"
    assert updated.pipeline_status == "done"
    state2 = __import__("litehive.tasks", fromlist=["load_state"]).load_state(root)
    assert task.id not in state2.queue


def test_apply_task_updates_with_action_park(tmp_path):
    root = ensure_workspace(tmp_path)
    task = create_task(root, title="Parked task", goal="Check parking")
    state = __import__("litehive.tasks", fromlist=["load_state"]).load_state(root)
    state.queue.append(task.id)
    __import__("litehive.tasks", fromlist=["save_state"]).save_state(root, state)

    report = StageReport(
        task_id=task.id,
        step="grooming",
        verdict="pass",
        summary="Parking for now.",
        task_update={
            "action": "park",
        },
    )

    apply_task_updates_from_report(root, task, report)

    updated = get_task(root, task.id)
    assert updated.status == "parked"
    assert updated.runtime.execution_status == "paused"
    state2 = __import__("litehive.tasks", fromlist=["load_state"]).load_state(root)
    assert task.id not in state2.queue


def test_runner_exits_cleanly_when_grooming_report_closes_task(tmp_path):
    from litehive.runner import TaskExecutionRunner
    from litehive.tasks import save_task

    root = ensure_workspace(tmp_path)
    task = create_task(root, title="Close mid-run", goal="To be closed")
    task.acceptance_criteria = ["Something verifiable"]
    save_task(root, task)

    call_count = 0

    def executor(task, step):
        nonlocal call_count
        call_count += 1
        if step == "grooming":
            return StageReport(
                task_id=task.id,
                step=step,
                verdict="pass",
                summary="Duplicate found.",
                task_update={
                    "outcome": "duplicate",
                    "outcome_reason": "Duplicate of T-0001",
                },
            )
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(root, executor)
    result = runner.run(task)

    assert result.final_status == "duplicate"
    assert call_count == 1


def test_runner_exits_cleanly_when_grooming_report_parks_task(tmp_path):
    from litehive.runner import TaskExecutionRunner
    from litehive.tasks import save_task

    root = ensure_workspace(tmp_path)
    task = create_task(root, title="Park mid-run", goal="To be parked")
    task.acceptance_criteria = ["Something verifiable"]
    save_task(root, task)

    call_count = 0

    def executor(task, step):
        nonlocal call_count
        call_count += 1
        if step == "grooming":
            return StageReport(
                task_id=task.id,
                step=step,
                verdict="pass",
                summary="Parking.",
                task_update={
                    "action": "park",
                },
            )
        return StageReport(task_id=task.id, step=step, verdict="pass", summary=f"{step} ok", files_changed=["app.txt"], tests={"added": 1, "passing": 1})

    runner = TaskExecutionRunner(root, executor)
    result = runner.run(task)

    assert result.final_status == "parked"
    assert call_count == 1
