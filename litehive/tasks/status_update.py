"""Task metadata edits and operator-intent routing.

``_update_task_transition`` edits task metadata in place or, when an
``outcome``/``action`` is supplied, dispatches to one of the terminal
transitions in ``status_close``/``status_resume``.
"""

from typing import cast

from litehive.domain.common import PipelineStatus
from litehive.domain.task import TaskRecord
from litehive.workspace import Workspace

from litehive.tasks.constants import (
    VALID_TASK_PRIORITIES,
)
from litehive.state.locking import (
    WorkspaceMutationGuard,
    WorkspaceRunnerLock,
    WorkspaceStateLock,
)
from litehive.state.persist import WorkspaceStateRepository
from litehive.state.records import WorkspaceTasks
from litehive.tasks.audit import build_task_audit_entry, snapshot_task_audit_state
from litehive.tasks.normalization import (
    missing_acceptance_criteria_reason,
    normalize_acceptance_criteria,
    normalize_task_text_list,
    reroute_stage_for_acceptance_criteria,
)
from litehive.tasks.queue import TaskQueueService
from litehive.tasks.status_close import _abandon_task_transition, _close_task_transition, _park_task_transition
from litehive.tasks.status_resume import _requeue_task_transition


def _update_task_transition(
    workspace: Workspace,
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
    """
    Edit task metadata or route the intent into a terminal transition.

    The ``...`` sentinels distinguish "leave field alone" from "set to
    None"; without them, an explicit clear (e.g. ``model=None``) would be
    indistinguishable from "no change" and the runner would never see the
    intended reset.
    """
    if outcome is not ... and outcome is not None:
        if outcome_reason is not ... and outcome_reason is not None:
            close_reason_arg = str(outcome_reason)
        else:
            close_reason_arg = None
        return _close_task_transition(
            workspace,
            task_id,
            outcome=str(outcome),
            reason=close_reason_arg,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    if action is not ... and action is not None:
        if action == "park":
            return _park_task_transition(
                workspace,
                task_id,
                reason="Task parked via structured report.",
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        if action == "requeue":
            return _requeue_task_transition(
                workspace,
                task_id,
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        if action == "abandon":
            return _abandon_task_transition(
                workspace,
                task_id,
                reason="Task abandoned via structured report.",
                audit_actor=audit_actor,
                audit_source=audit_source,
            )
        raise ValueError(f"Unsupported action '{action}'")

    with WorkspaceStateLock(workspace).hold():
        state = WorkspaceStateRepository(workspace).load()
        task = WorkspaceTasks(workspace).get_record(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        before_task = snapshot_task_audit_state(task)
        queue_before = list(state.queue)
        # Skip the conflict guard when the current thread is the runner
        # (e.g., apply_task_updates_from_report during grooming).
        is_runner_thread = WorkspaceRunnerLock(workspace).owns_current_thread()
        allow_active_task_mutation = allow_active_agent_task_mutation and state.active_task_id == task.id
        if not is_runner_thread and not allow_active_task_mutation:
            WorkspaceMutationGuard(workspace).ensure_future_task_mutation_allowed([task.id], state=state)

        if depends_on is not ...:
            depends_on_list = cast(list[str], depends_on)
            TaskQueueService(workspace).validate_dependencies(
                task_id=task.id,
                depends_on=list(depends_on_list),
            )
            task.depends_on = list(depends_on_list)

        if title is not ...:
            task.title = str(title)


        if model is not ...:
            task.model = cast("str | None", model)

        if retry_limit is not ...:
            retry_limit_val = cast("int | None", retry_limit)
            if retry_limit_val is not None and retry_limit_val < 0:
                raise ValueError("Retry limit must be 0 or greater")
            task.retry_policy.max_retries = retry_limit_val

        if priority is not ...:
            priority_val = cast(str, priority)
            if priority_val not in VALID_TASK_PRIORITIES:
                raise ValueError(f"Unsupported priority '{priority_val}'")
            task.priority = priority_val

        if goal is not ...:
            task.goal = cast(str, goal)

        if acceptance_criteria is not ...:
            task.acceptance_criteria = normalize_acceptance_criteria(list(cast(list[str], acceptance_criteria)))

        if constraints is not ...:
            task.constraints = normalize_task_text_list(list(cast(list[str], constraints)))

        if plan is not ...:
            task.plan = normalize_task_text_list(list(cast(list[str], plan)))

        if auto_commit is not ...:
            task.git.auto_commit = cast(bool, auto_commit)

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
        WorkspaceMutationGuard(workspace).persist_future_task_update(
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
