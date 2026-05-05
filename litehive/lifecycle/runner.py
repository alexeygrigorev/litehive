import time
from typing import Callable

from litehive.domain.common import PipelineState
from litehive.lifecycle.persistence import CommitResult, FailedRunRecord
from litehive.domain.lifecycle_deltas import StateDelta
from .events import Event, HookOk, Pass, Reject, TaskTimeBudgetExceeded
from .journal import NullJournal, PipelineJournal
from .nodes.base import NodeRegistry
from .persistence import Persistence, TaskState
from .transitions import Rule, Transition, evaluate
from .rules import RULES
from .types import AGENT_STAGES, TERMINAL_NODES, NodeType, pipeline_stage_for_phase


StopPredicate = Callable[[], bool]
StateSyncCallback = Callable[[TaskState], None]
TransitionObserver = Callable[[TaskState, str, Event, Transition], None]
Clock = Callable[[], float]

_COMMIT_REACHED_PHASES = frozenset(
    {
        PipelineState.COMMIT,
        PipelineState.AFTER_COMMIT,
        PipelineState.MERGE_RESOLVING,
    }
)


def _normalize_report_string_list(items: list) -> list[str]:
    """
    Coerce a metadata list into stripped non-empty strings.

    The runner's ``Pass`` metadata can carry path-like or
    test-result entries that are :class:`pathlib.Path` instances,
    ``None``, or empty strings; this normalizer trims and drops
    blanks so the persisted ``LastReport`` only stores meaningful
    rows. Caller: :meth:`StateMachineRunner._update_last_report`.
    """
    cleaned: list[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


class StateMachineRunner:
    """Drives a task through the state machine.

    Holds the rule list, the node registry, and a ``PipelineJournal``. On
    each step it:
      1. asks the current node for an event (``Node.run``)
      2. evaluates the rules to get a ``Transition``
      3. applies the ``StateDelta`` to the task state
      4. persists the state and records the transition in the journal

    All logging lives inside the journal — the runner never calls ``logger``
    directly. Complexity for agent execution (retry / engine switch / session
    handling / nudges) lives inside the node subclasses. Routing complexity
    lives inside the rules (guards, effects). The runner itself is the loop.
    """

    def __init__(
        self,
        registry: NodeRegistry,
        persistence: Persistence,
        rules: list[Rule] = RULES,
        journal: PipelineJournal | None = None,
        session_store=None,
        stop_requested: StopPredicate | None = None,
        state_sync: StateSyncCallback | None = None,
        transition_observer: TransitionObserver | None = None,
        task_time_budget_seconds: float | None = None,
        clock: Clock | None = None,
    ) -> None:
        """
        Wire the runner with the dependencies it cannot resolve itself.

        Receives the node registry, persistence layer, journal, and the
        operator-controlled knobs (stop predicate, time budget, and the
        two observability hooks). Constructed once per task launch by
        ``orchestration.run_task``; the registry and rules are otherwise
        immutable for the run.
        """
        self.registry = registry
        self.persistence = persistence
        self.rules = rules
        self.journal = journal or NullJournal()
        self.session_store = session_store
        self._stop_requested = stop_requested or (lambda: False)
        self._state_sync = state_sync
        self._transition_observer = transition_observer
        self._task_time_budget_seconds = task_time_budget_seconds
        self._clock = clock or time.monotonic

    def run_task(self, task_id: str) -> TaskState:
        """
        Drive a single task through the state machine to completion.

        Loops until the task hits a terminal node, the operator
        ``stop_requested`` predicate fires, or the cumulative agent
        time budget is exceeded. Called by the daemon's per-task worker
        loop and by ``orchestration.run_task`` for the one-shot CLI.
        """
        state = self.persistence.load(task_id)
        self.journal.task_started(task_id, state.stage)
        while state.stage not in TERMINAL_NODES:
            budget_event = self._task_time_budget_event(state)
            if budget_event is not None:
                self._apply_transition(state, from_stage=state.stage, event=budget_event, task_id=task_id)
                continue
            from_stage = state.stage
            node = self.registry.get(from_stage)
            if node.node_type == NodeType.AGENT:
                started_at = self._clock()
            else:
                started_at = None
            event = node.run(state)
            if started_at is not None:
                state.agent_elapsed_seconds += max(0.0, self._clock() - started_at)
            self._apply_transition(state, from_stage=from_stage, event=event, task_id=task_id)
            if self._stop_requested():
                self.journal.stop_requested(task_id, state.stage)
                return state
        self.journal.task_finished(task_id, state.stage)
        return state

    def _task_time_budget_event(
        self,
        state: TaskState,
    ) -> TaskTimeBudgetExceeded | None:
        """
        Synthesize a budget-exceeded event when the task has burned
        its agent-time allowance.

        Only fires before commit; once the task has reached
        commit/after_commit/merge_resolving we let it finish rather
        than abandon real progress that's about to land. ``None`` means
        no synthetic event — the run loop continues normally.
        """
        budget = self._task_time_budget_seconds
        if budget is None:
            return None
        if state.stage in _COMMIT_REACHED_PHASES:
            return None
        if state.agent_elapsed_seconds < budget:
            return None
        return TaskTimeBudgetExceeded(
            elapsed_seconds=state.agent_elapsed_seconds,
            budget_seconds=budget,
        )

    def _apply_transition(
        self,
        state: TaskState,
        from_stage: PipelineState,
        event: Event,
        task_id: str,
    ) -> None:
        """
        Run one rule evaluation and commit its consequences in order.

        Applies the StateDelta, walks event side-effects, persists the
        new state, notifies observers, then records the transition in
        the journal. Keeping every consequence in one method is what
        keeps ``run_task`` itself a thin orchestrator that only walks
        from node to node.
        """
        trans = evaluate(self.rules, from_stage, event, state)
        self._apply_delta(state, trans.delta)
        self.apply_event_side_effects(state, event)
        state.stage = trans.next
        self._reset_cross_agent_retry_sessions(
            task_id=state.task_id,
            from_stage=from_stage,
            to_stage=trans.next,
            event=event,
        )
        self._reset_hook_reject_tracking_on_progress(state, from_stage, trans.next, event)
        self.persistence.save(state)
        if self._state_sync is not None:
            self._state_sync(state)
        self.journal.transition(
            task_id=task_id,
            from_stage=from_stage,
            event=event,
            to_stage=trans.next,
            rule_description=trans.rule.description,
            delta=trans.delta,
        )
        if self._transition_observer is not None:
            self._transition_observer(state, from_stage, event, trans)

    @staticmethod
    def apply_event_side_effects(state: TaskState, event: Event) -> None:
        """Apply metadata that rides on the event rather than a StateDelta.

        ``HookOk`` marks the latest hook run as green; hook Rejects clear
        that bit. ``Pass.metadata`` carries agent-reported ``files_changed`` /
        ``tests_added``, and the Runner keeps ``state.last_report`` in sync so
        downstream guards see real numbers instead of defaults.
        """
        if isinstance(event, HookOk):
            state.last_report.hook_ok = True
            return
        if isinstance(event, Reject) and event.source == "hook":
            state.last_report.hook_ok = False
            return
        if not isinstance(event, Pass):
            return
        meta = event.metadata or {}
        files_changed = meta.get("files_changed")
        if isinstance(files_changed, list):
            state.last_report.files_changed = len(files_changed)
            state.last_report.changed_files = _normalize_report_string_list(files_changed)
        elif isinstance(files_changed, int):
            state.last_report.files_changed = files_changed
        tests_added = meta.get("tests_added")
        if isinstance(tests_added, int):
            state.last_report.tests_added = tests_added
        last_report = meta.get("last_report")
        if isinstance(last_report, dict):
            changed_files = last_report.get("changed_files")
            if isinstance(changed_files, list):
                state.last_report.changed_files = _normalize_report_string_list(changed_files)
            test_results = last_report.get("test_results")
            if isinstance(test_results, list):
                state.last_report.test_results = _normalize_report_string_list(test_results)
        commit_result = meta.get("commit_result")
        if isinstance(commit_result, dict):
            head_sha = commit_result.get("head_sha")
            if isinstance(head_sha, str) and head_sha:
                state.commit_result = CommitResult(
                    head_sha=head_sha,
                    reason=commit_result.get("reason"),
                )

    @staticmethod
    def _reset_hook_reject_tracking_on_progress(
        state: TaskState,
        from_stage: str,
        to_stage: str,
        event: Event,
    ) -> None:
        """
        Clear the hook-reject streak once the task has actually moved
        forward.

        Without this, a later rejection at a different agent stage
        would inherit a stale streak from the previous stage and the
        circuit breaker would fail the task on what is really a fresh
        problem.
        """
        if not isinstance(event, (Pass, HookOk)):
            return
        if pipeline_stage_for_phase(from_stage) == pipeline_stage_for_phase(to_stage):
            if from_stage not in {PipelineState.COMMIT, PipelineState.MERGE_RESOLVING}:
                return
        StateMachineRunner._clear_hook_reject_tracking(state, clear_recovery_invoked=True)

    @staticmethod
    def _clear_hook_reject_tracking(
        state: TaskState,
        clear_recovery_invoked: bool,
    ) -> None:
        """
        Reset the hook-reject streak counters.

        ``clear_recovery_invoked`` distinguishes "real progress" (clear
        everything, including the recovery-was-tried bit) from
        "delta-driven clear" (the rule asked for a reset but recovery
        may still be in flight, so the recovery-tried flag must
        survive).
        """
        state.consecutive_same_hook_rejects = 0
        state.last_hook_reject_fingerprint = None
        if clear_recovery_invoked:
            state.hook_reject_recovery_invoked = False

    @staticmethod
    def _apply_delta(state: TaskState, delta: StateDelta) -> None:
        """
        Translate the rule-produced StateDelta into concrete TaskState
        mutations.

        This is the only place fields like retry counters, recovery
        triggers, rejection-loop tracking, and failure metadata are
        written, so rules can stay declarative — they emit the delta
        they want; this method is the single applier.
        """
        if delta.inc_stage_retry is not None:
            stage = delta.inc_stage_retry
            state.stage_retry[stage] = state.stage_retry.get(stage, 0) + 1
        if delta.reset_stage_retry is not None:
            state.stage_retry.pop(delta.reset_stage_retry, None)
        if delta.set_active_recovery_trigger is not None:
            state.active_recovery_trigger = delta.set_active_recovery_trigger
        if delta.clear_active_recovery_trigger:
            state.active_recovery_trigger = None
        if delta.append_recovery_outcome is not None:
            state.recovery_history.append(delta.append_recovery_outcome)
        if delta.inc_pre_exec_recovery_attempt:
            state.pre_exec_recovery_attempt += 1
        if delta.set_merge_context is not None:
            state.merge_context = delta.set_merge_context
        if delta.clear_merge_context:
            state.merge_context = None
        if delta.set_last_rejection is not None:
            stage, rejection = delta.set_last_rejection
            state.last_rejection_by_stage[stage] = rejection
        if delta.clear_rejection_loop:
            state.rejection_loop = None
        if delta.set_rejection_loop is not None:
            state.rejection_loop = delta.set_rejection_loop
        if delta.clear_hook_reject_tracking:
            StateMachineRunner._clear_hook_reject_tracking(state, clear_recovery_invoked=False)
        if delta.set_consecutive_same_hook_rejects is not None:
            state.consecutive_same_hook_rejects = delta.set_consecutive_same_hook_rejects
        if delta.set_last_hook_reject_fingerprint is not None:
            state.last_hook_reject_fingerprint = delta.set_last_hook_reject_fingerprint
        if delta.set_hook_reject_recovery_invoked is not None:
            state.hook_reject_recovery_invoked = delta.set_hook_reject_recovery_invoked
        if delta.failed_reason is not None:
            state.failed_reason = delta.failed_reason
        if delta.failed_message is not None:
            state.failed_message = delta.failed_message
        if delta.clear_recovery_failure_explanation:
            state.recovery_failure_explanation = None
        if delta.set_recovery_failure_explanation is not None:
            state.recovery_failure_explanation = delta.set_recovery_failure_explanation
        if delta.record_failed_run is not None:
            StateMachineRunner._record_failed_run(state, delta.record_failed_run)

    @staticmethod
    def _record_failed_run(state: TaskState, record: FailedRunRecord) -> None:
        """
        Merge a new failed-run report into the per-failure-shape
        history.

        Operator-override bookkeeping (``operator_override_count``,
        ``last_operator_override_at``) is preserved on the existing
        row so it cannot be silently zeroed by a fresh failure.
        Called from ``_apply_delta`` when a rule emits
        ``record_failed_run`` so the recovery agent can see how many
        times the same shape has recurred.
        """
        existing = state.failed_run_history.get(record.key)
        if existing is None:
            state.failed_run_history[record.key] = record
            return
        state.failed_run_history[record.key] = FailedRunRecord(
            stage=record.stage,
            failure_shape=record.failure_shape,
            count=existing.count + 1,
            first_at=existing.first_at or record.first_at,
            latest_at=record.latest_at,
            last_reason=record.last_reason,
            source=record.source,
            classification=record.classification,
            retry_limit=record.retry_limit,
            failed_reason=record.failed_reason,
            operator_override_count=existing.operator_override_count,
            last_operator_override_at=existing.last_operator_override_at,
        )

    def _reset_cross_agent_retry_sessions(
        self,
        task_id: str,
        from_stage: str,
        to_stage: str,
        event: Event,
    ) -> None:
        """
        Drop saved subagent sessions for the destination stage when a
        reject hands control across agents.

        Without this, the next agent would resume a conversation that
        was rejected at *someone else's* stage and inherit irrelevant
        context; same-agent retries are deliberately not affected so
        the engine's own resume flag still works inside one stage.
        """
        if self.session_store is None or not isinstance(event, Reject) or event.source != "agent":
            return
        from_agent_stage = pipeline_stage_for_phase(from_stage)
        to_agent_stage = pipeline_stage_for_phase(to_stage)
        if from_agent_stage == to_agent_stage:
            return
        if from_agent_stage not in AGENT_STAGES or to_agent_stage not in AGENT_STAGES:
            return
        self.session_store.clear_node_sessions(task_id, to_agent_stage)
