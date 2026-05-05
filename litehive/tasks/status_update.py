"""Task metadata edits and operator-intent routing.

``update_task`` is the public entry that either edits task metadata in
place or, when an ``outcome``/``action`` is supplied, dispatches to one
of the terminal transitions in ``status_close``/``status_resume``.
"""

import threading
from pathlib import Path

from litehive.domain.common import PipelineStatus
from litehive.domain.task import TaskRecord

from litehive.tasks.constants import (
    VALID_TASK_PRIORITIES,
    RUNNER_LOCKS,
    RUNNER_LOCKS_MUTEX,
)
from litehive.state.locking import (
    ensure_future_task_mutation_allowed,
    persist_future_task_update,
    workspace_lock,
)
from litehive.state.persist import load_state
from litehive.state.records import get_task_record
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.normalization import (
    missing_acceptance_criteria_reason,
    normalize_acceptance_criteria,
    normalize_task_text_list,
    reroute_stage_for_acceptance_criteria,
)
from litehive.tasks.queue import validate_task_dependencies
from litehive.tasks.status_close import abandon_task, close_task, park_task
from litehive.tasks.status_resume import requeue_task


def _update_task_transition(
    root: Path,
    task_id: str,
    title: str | object = ...,
    depends_on: list[str] | object = ...,
    model: str | None | object = ...,
    retry_limit: int | None | object = ...,
    priority: str | object = ...,
    goal: str | object = ...,
    acceptance_criteria: list[str] | object = ...,
    constraints: list[str] | object = ...,
    plan: list[str] | object = ...,
    auto_commit: bool | object = ...,
    outcome: str | None | object = ...,
    outcome_reason: str | None | object = ...,
    action: str | None | object = ...,
    allow_active_agent_task_mutation: bool = False,
    journal_message: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """Edit task metadata or route the operator's intent into a terminal transition (close/park/requeue/abandon); uses ``...`` sentinels so callers can distinguish "leave field alone" from "set to None"."""

    if outcome is not ... and outcome is not None:
        if outcome_reason is not ... and outcome_reason is not None:
            close_reason_arg = str(outcome_reason)
        else:
            close_reason_arg = None
        return close_task(
            root,
            task_id,
            outcome=str(outcome),
            reason=close_reason_arg,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    if action is not ... and action is not None:
        if action == "park":
            return park_task(
                root,
                task_id,
                reason="Task parked via structured report.",
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        if action == "requeue":
            return requeue_task(
                root,
                task_id,
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        if action == "abandon":
            return abandon_task(
                root,
                task_id,
                reason="Task abandoned via structured report.",
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        raise ValueError(f"Unsupported action '{action}'")

    with workspace_lock(root):
        state = load_state(root)
        task = get_task_record(root, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        queue_before = list(state.queue)
        # Skip the conflict guard when the current thread is the runner
        # (e.g., apply_task_updates_from_report during grooming).
        owner_thread_id = threading.get_ident()
        with RUNNER_LOCKS_MUTEX:
            runner_state = RUNNER_LOCKS.get(root.resolve())
        is_runner_thread = runner_state is not None and runner_state.owner_thread_id == owner_thread_id
        allow_active_task_mutation = allow_active_agent_task_mutation and state.active_task_id == task.id
        if not is_runner_thread and not allow_active_task_mutation:
            ensure_future_task_mutation_allowed(root, [task.id], state=state)

        if depends_on is not ...:
            validate_task_dependencies(root, task_id=task.id, depends_on=list(depends_on))
            task.depends_on = list(depends_on)

        if title is not ...:
            task.title = str(title)


        if model is not ...:
            task.model = model

        if retry_limit is not ...:
            if retry_limit is not None and retry_limit < 0:
                raise ValueError("Retry limit must be 0 or greater")
            task.retry_policy.max_retries = retry_limit

        if priority is not ...:
            if priority not in VALID_TASK_PRIORITIES:
                raise ValueError(f"Unsupported priority '{priority}'")
            task.priority = priority

        if goal is not ...:
            task.goal = goal

        if acceptance_criteria is not ...:
            task.acceptance_criteria = normalize_acceptance_criteria(list(acceptance_criteria))

        if constraints is not ...:
            task.constraints = normalize_task_text_list(list(constraints))

        if plan is not ...:
            task.plan = normalize_task_text_list(list(plan))

        if auto_commit is not ...:
            task.git.auto_commit = auto_commit

        task.pipeline_status = reroute_stage_for_acceptance_criteria(task)
        changed_fields = [
            name
            for name, changed in (
                ("depends_on", depends_on is not ...),
                ("title", title is not ...),
                ("model", model is not ...),
                ("retry_limit", retry_limit is not ...),
                ("priority", priority is not ...),
                ("goal", goal is not ...),
                ("acceptance_criteria", acceptance_criteria is not ...),
                ("constraints", constraints is not ...),
                ("plan", plan is not ...),
                ("auto_commit", auto_commit is not ...),
            )
            if changed
        ]

        if journal_message is None:
            journal_message = "Task metadata updated via CLI."
        if task.pipeline_status == PipelineStatus.GROOMING and missing_acceptance_criteria_reason(task) is not None:
            journal_message += " Rerouted to `grooming` until structured acceptance criteria are added."
        persist_future_task_update(
            root,
            task,
            journal_message=journal_message,
            audit_entries=[
                build_task_audit_entry(
                    task_id=task.id,
                    action="metadata_updated",
                    actor=audit_actor,
                    source=audit_source,
                    before_task=before_task,
                    after_task=task,
                    before_queue=queue_before,
                    after_queue=state.queue,
                    context={"changed_fields": changed_fields},
                )
            ],
        )
        return task


def update_task(
    root: Path,
    task_id: str,
    title: str | object = ...,
    depends_on: list[str] | object = ...,
    model: str | None | object = ...,
    retry_limit: int | None | object = ...,
    priority: str | object = ...,
    goal: str | object = ...,
    acceptance_criteria: list[str] | object = ...,
    constraints: list[str] | object = ...,
    plan: list[str] | object = ...,
    auto_commit: bool | object = ...,
    outcome: str | None | object = ...,
    outcome_reason: str | None | object = ...,
    action: str | None | object = ...,
    allow_active_agent_task_mutation: bool = False,
    journal_message: str | None = None,
    audit_actor: str = "operator",
    audit_source: str = "cli",
) -> TaskRecord:
    """Public CLI/agent entry that either edits task metadata or routes the operator's intent into the matching terminal transition (close/park/requeue/abandon)."""
    return _update_task_transition(
        root,
        task_id,
        title=title,
        depends_on=depends_on,
        model=model,
        retry_limit=retry_limit,
        priority=priority,
        goal=goal,
        acceptance_criteria=acceptance_criteria,
        constraints=constraints,
        plan=plan,
        auto_commit=auto_commit,
        outcome=outcome,
        outcome_reason=outcome_reason,
        action=action,
        allow_active_agent_task_mutation=allow_active_agent_task_mutation,
        journal_message=journal_message,
        audit_actor=audit_actor,
        audit_source=audit_source,
    )
