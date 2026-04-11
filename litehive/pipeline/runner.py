from typing import Callable

from .deltas import StateDelta
from .journal import NullJournal, PipelineJournal
from .nodes.base import NodeRegistry
from .persistence import Persistence, TaskState
from .transitions import RULES, Rule, Transition, evaluate
from .types import NodeName, TERMINAL_NODES


StopPredicate = Callable[[], bool]


class StateMachineRunner:
    """Drives a task through the state machine.

    Holds the rule list, the node registry, and a ``PipelineJournal``. On
    each step it:
      1. asks the current node for an event (``Node.run``)
      2. evaluates the rules to get a ``Transition``
      3. applies the ``StateDelta`` to the task state
      4. persists the state and records the transition in the journal

    All logging lives inside the journal — the runner never calls ``logger``
    directly. Complexity for agent execution (tier-1/tier-2, session handling,
    engine fallback) lives inside the node subclasses. Routing complexity
    lives inside the rules (guards, effects). The runner itself is the loop.
    """

    def __init__(
        self,
        registry: NodeRegistry,
        persistence: Persistence,
        *,
        rules: list[Rule] = RULES,
        journal: PipelineJournal | None = None,
        stop_requested: StopPredicate | None = None,
    ) -> None:
        self.registry = registry
        self.persistence = persistence
        self.rules = rules
        self.journal = journal or NullJournal()
        self._stop_requested = stop_requested or (lambda: False)

    def run_task(self, task_id: str) -> TaskState:
        state = self.persistence.load(task_id)
        self.journal.task_started(task_id, state.stage)
        while state.stage not in TERMINAL_NODES:
            from_stage = state.stage
            node = self.registry.get(from_stage)
            event = node.run(state)
            trans = evaluate(self.rules, from_stage, event, state)
            self._apply_delta(state, trans.delta)
            state.stage = trans.next
            self.persistence.save(state)
            self.journal.transition(
                task_id=task_id,
                from_stage=from_stage,
                event=event,
                to_stage=trans.next,
                rule_description=trans.rule.description,
                delta=trans.delta,
            )
            if self._stop_requested():
                self.journal.stop_requested(task_id, state.stage)
                return state
        self.journal.task_finished(task_id, state.stage)
        return state

    @staticmethod
    def _apply_delta(state: TaskState, delta: StateDelta) -> None:
        if delta.set_origin_stage is not None:
            state.origin_stage = delta.set_origin_stage
        if delta.clear_origin_stage:
            state.origin_stage = None
        if delta.inc_stage_retry is not None:
            stage = delta.inc_stage_retry
            state.stage_retry[stage] = state.stage_retry.get(stage, 0) + 1
        if delta.reset_stage_retry is not None:
            state.stage_retry.pop(delta.reset_stage_retry, None)
        if delta.inc_recovery_attempt is not None:
            stage = delta.inc_recovery_attempt
            state.recovery_attempt[stage] = state.recovery_attempt.get(stage, 0) + 1
        if delta.inc_pre_exec_recovery_attempt:
            state.pre_exec_recovery_attempt += 1
        if delta.set_last_rejection is not None:
            stage, rejection = delta.set_last_rejection
            state.last_rejection_by_stage[stage] = rejection
        if delta.set_failure_context is not None:
            state.failure_context = dict(delta.set_failure_context)
        if delta.failed_reason is not None:
            state.failed_reason = delta.failed_reason
        if delta.failed_message is not None:
            state.failed_message = delta.failed_message
