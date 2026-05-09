"""Task status transitions: requeue, resume, abandon, close, park, update, stop, switch.

Historically this module contained every status-mutation helper in one
place. It now exposes the workspace-bound ``TaskStatusService``; focused
sibling modules keep the private transition implementations.
"""

from litehive.domain.task import TaskRecord
from litehive.domain.task_ops import StopTaskSummary
from litehive.tasks.completed_task_recovery import _recover_completed_task_transition
from litehive.tasks.status_close import (
    _abandon_task_transition,
    _close_task_transition,
    _park_task_transition,
)
from litehive.tasks.status_resume import _requeue_task_transition, _resume_task_transition
from litehive.tasks.status_update import _update_task_transition
from litehive.tasks.stop import _stop_current_task
from litehive.tasks.switch_engine import SwitchTaskSummary, _switch_task_engine_impl
from litehive.workspace import Workspace


class TaskStatusService:
    """
    Workspace-bound owner for operator task-status transitions.

    The transition modules still hold focused implementation bodies during
    migration. Production callers should use this service so status mutations
    have a single object boundary instead of importing workspace-first
    functions directly.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def abandon(
        self,
        task_id: str,
        reason: str = "Task abandoned via CLI.",
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _abandon_task_transition(
            self.workspace,
            task_id,
            reason=reason,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    def close(
        self,
        task_id: str,
        outcome: str,
        reason: str | None = None,
        follow_up_task_id: str | None = None,
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _close_task_transition(
            self.workspace,
            task_id,
            outcome=outcome,
            reason=reason,
            follow_up_task_id=follow_up_task_id,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    def park(
        self,
        task_id: str,
        reason: str = "Task parked via CLI.",
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _park_task_transition(
            self.workspace,
            task_id,
            reason=reason,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    def requeue(
        self,
        task_id: str,
        front: bool = False,
        force: bool = False,
        audit_actor: str = "operator",
        audit_source: str = "cli",
    ) -> TaskRecord:
        return _requeue_task_transition(
            self.workspace,
            task_id,
            front=front,
            force=force,
            audit_actor=audit_actor,
            audit_source=audit_source,
        )

    def resume(self, task_id: str, front: bool = False) -> TaskRecord:
        return _resume_task_transition(self.workspace, task_id, front=front)

    def recover_completed(self, task_id: str) -> TaskRecord:
        return _recover_completed_task_transition(self.workspace, task_id)

    def stop_current(self) -> StopTaskSummary:
        return _stop_current_task(self.workspace)

    def switch_engine(self, task_id: str, engine: str, reason: str) -> SwitchTaskSummary:
        return _switch_task_engine_impl(self.workspace, task_id, engine=engine, reason=reason)

    def update(
        self,
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
        return _update_task_transition(
            self.workspace,
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

__all__ = [
    "TaskStatusService",
]
