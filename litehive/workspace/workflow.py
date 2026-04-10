"""Task state transition and persistence helpers."""

from pathlib import Path

from litehive.config import state_path
from litehive.models import TaskRecord, WorkspaceState, utcnow
from litehive.storage import runtime_store

from litehive.workspace.locking import workspace_mutation_guard
from litehive.tasks.paths import task_dir, task_file, task_runtime_file
from litehive.tasks.persistence import (
    _serialize_state,
    _write_atomic_files,
    load_state,
)


def _workspace_transition_writes(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...] = (),
    state: WorkspaceState | None = None,
    journal_messages: dict[str, str] | None = None,
) -> dict[Path, str]:
    from litehive.tasks.crud import _serialize_task_record, _serialize_task_runtime

    writes: dict[Path, str] = {}
    for task in tasks:
        writes[task_file(root, task)] = _serialize_task_record(task)
        writes[task_runtime_file(root, task)] = _serialize_task_runtime(task)
        if journal_messages is None or task.id not in journal_messages:
            continue
        journal_path = task_dir(root, task) / "journal.md"
        existing = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        writes[journal_path] = f"{existing}\n## {utcnow()}\n{journal_messages[task.id]}\n"
    if state is not None:
        state = _merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in tasks],
        )
        writes[state_path(root)] = _serialize_state(state)
    return writes


def _merge_queue_preserving_future_changes(
    *,
    desired_queue: list[str],
    latest_queue: list[str],
    protected_task_ids: list[str] | tuple[str, ...],
) -> list[str]:
    protected: list[str] = []
    seen_protected: set[str] = set()
    for task_id in protected_task_ids:
        if task_id in seen_protected:
            continue
        seen_protected.add(task_id)
        protected.append(task_id)
    if not protected:
        return list(desired_queue)

    protected_set = set(protected)
    latest_unprotected = [task_id for task_id in latest_queue if task_id not in protected_set]
    protected_positions = [
        (
            sum(1 for preceding in desired_queue[:index] if preceding not in protected_set),
            task_id,
        )
        for index, task_id in enumerate(desired_queue)
        if task_id in protected_set
    ]
    if not protected_positions:
        return list(latest_unprotected)

    merged = list(latest_unprotected)
    inserted = 0
    inserted_ids: set[str] = set()
    for unprotected_before, task_id in protected_positions:
        if task_id in inserted_ids:
            continue
        insertion_index = min(unprotected_before + inserted, len(merged))
        merged.insert(insertion_index, task_id)
        inserted_ids.add(task_id)
        inserted += 1
    return merged


def _merged_state_for_runner_owned_write(
    root: Path,
    *,
    state: WorkspaceState,
    protected_task_ids: list[str] | tuple[str, ...] = (),
) -> WorkspaceState:
    latest_state = load_state(root)
    merged_state = state.model_copy(deep=True)
    merged_state.queue = _merge_queue_preserving_future_changes(
        desired_queue=state.queue,
        latest_queue=latest_state.queue,
        protected_task_ids=protected_task_ids,
    )
    merged_state.next_task_number = max(state.next_task_number, latest_state.next_task_number)
    return merged_state


def apply_task_updates_from_report(root: Path, task: TaskRecord, report: "object") -> bool:
    """Apply structured and text-based task updates from a stage report."""
    from litehive.cli._parse import _parse_rich_task_update_document

    from litehive.tasks.constants import VALID_PLANNED_EFFORTS, VALID_PM_COMPLEXITIES
    from litehive.tasks.journal import append_journal
    from litehive.tasks.normalization import (
        extract_report_line,
        extract_report_list_section,
        infer_acceptance_criteria,
        normalize_acceptance_criteria,
        normalize_task_text_list,
    )
    from litehive.workspace.task_status import update_task

    updates: dict[str, object] = {}

    # Preferred: structured task_update from STAGE_RESULT or TASK_UPDATE YAML block.
    if report.task_update:
        try:
            updates = _parse_rich_task_update_document(
                report.task_update, source="report task_update"
            )
        except ValueError as exc:
            append_journal(root, task, f"Warning: Ignoring malformed task update in report: {exc}")

    # Fallback/Supplemental: text-based list sections.
    feedback = report.feedback
    if "acceptance_criteria" not in updates:
        text_criteria = normalize_acceptance_criteria(
            extract_report_list_section(feedback, "ACCEPTANCE_CRITERIA")
        )
        if not text_criteria and not task.acceptance_criteria:
            text_criteria = infer_acceptance_criteria(task)
        if text_criteria and not task.acceptance_criteria:
            updates["acceptance_criteria"] = text_criteria

    if "constraints" not in updates:
        text_constraints = normalize_task_text_list(
            extract_report_list_section(feedback, "CONSTRAINTS")
        )
        if text_constraints and not task.constraints:
            updates["constraints"] = text_constraints

    if "plan" not in updates:
        text_plan = normalize_task_text_list(extract_report_list_section(feedback, "PLAN"))
        if text_plan and not task.plan:
            updates["plan"] = text_plan

    # Fallback/Supplemental: PM sizing lines.
    pm_complexity = extract_report_line(feedback, "PM_COMPLEXITY")
    if "pm_complexity" not in updates and pm_complexity in VALID_PM_COMPLEXITIES:
        updates["pm_complexity"] = pm_complexity

    planned_effort = extract_report_line(feedback, "PLANNED_EFFORT")
    if "planned_effort" not in updates and planned_effort in VALID_PLANNED_EFFORTS:
        updates["planned_effort"] = planned_effort

    if not updates:
        return False

    journal_message = "Task record updated from grooming output:\n" + "\n".join(
        f"- {key}: `{value}`" for key, value in updates.items()
    )

    # Use update_task logic to apply updates.
    updated = update_task(
        root,
        task.id,
        goal=updates.get("goal", ...),
        acceptance_criteria=updates.get("acceptance_criteria", ...),
        constraints=updates.get("constraints", ...),
        plan=updates.get("plan", ...),
        pm_complexity=updates.get("pm_complexity", ...),
        planned_effort=updates.get("planned_effort", ...),
        depends_on=updates.get("depends_on", ...),
        human_checkpoints=updates.get("human_checkpoints", ...),
        task_type=updates.get("task_type", ...),
        mode=updates.get("mode", ...),
        priority=updates.get("priority", ...),
        model=updates.get("model", ...),
        retry_limit=updates.get("retry_limit", ...),
        auto_commit=updates.get("auto_commit", ...),
        outcome=updates.get("outcome", ...),
        outcome_reason=updates.get("outcome_reason", ...),
        action=updates.get("action", ...),
        journal_message=journal_message,
    )

    # Re-sync the passed-in task object so the caller sees the changes.
    for field_name in updated.__class__.model_fields:
        if field_name == "runtime":
            continue
        setattr(task, field_name, getattr(updated, field_name))

    return True


def persist_task_and_state(
    root: Path,
    *,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str | None = None,
) -> None:
    persist_tasks_and_state(
        root,
        tasks=[task],
        state=state,
        journal_messages={task.id: journal_message} if journal_message is not None else None,
    )


def persist_tasks_and_state(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
) -> None:
    from litehive.tasks.crud import _ensure_runtime_ignored

    for task in tasks:
        task.updated_at = utcnow()
    writes = _workspace_transition_writes(
        root,
        tasks=tasks,
        state=state,
        journal_messages=journal_messages,
    )
    with workspace_mutation_guard(root):
        _write_atomic_files(writes)
        from litehive.tasks.crud import _task_runtime_for_storage

        store = runtime_store(root)
        for task in tasks:
            store.save_task_runtime(task.id, _task_runtime_for_storage(task))
        merged_state = _merged_state_for_runner_owned_write(
            root,
            state=state,
            protected_task_ids=[task.id for task in tasks],
        )
        store.save_workspace_state(merged_state)
        _ensure_runtime_ignored(root)


def _persist_tasks_and_state_without_runner_guard(
    root: Path,
    *,
    tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    state: WorkspaceState,
    journal_messages: dict[str, str] | None = None,
) -> None:
    from litehive.tasks.crud import _ensure_runtime_ignored

    for task in tasks:
        task.updated_at = utcnow()
    writes = _workspace_transition_writes(
        root,
        tasks=tasks,
        state=state,
        journal_messages=journal_messages,
    )
    _write_atomic_files(writes)
    from litehive.tasks.crud import _task_runtime_for_storage

    store = runtime_store(root)
    for task in tasks:
        store.save_task_runtime(task.id, _task_runtime_for_storage(task))
    merged_state = _merged_state_for_runner_owned_write(
        root,
        state=state,
        protected_task_ids=[task.id for task in tasks],
    )
    store.save_workspace_state(merged_state)
    _ensure_runtime_ignored(root)


def _persist_task_and_state_without_runner_guard(
    root: Path,
    *,
    task: TaskRecord,
    state: WorkspaceState,
    journal_message: str | None = None,
) -> None:
    _persist_tasks_and_state_without_runner_guard(
        root,
        tasks=[task],
        state=state,
        journal_messages={task.id: journal_message} if journal_message is not None else None,
    )
