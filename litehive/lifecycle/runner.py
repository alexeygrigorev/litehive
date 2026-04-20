from typing import Callable

from litehive.lifecycle.persistence import CommitResult
from litehive.domain.lifecycle_deltas import StateDelta
from .events import Event, HookOk, Pass, Reject
from .journal import NullJournal, PipelineJournal
from .nodes.base import NodeRegistry
from .persistence import Persistence, TaskState
from .transitions import Rule, Transition, evaluate
from .rules import RULES
from .types import AGENT_STAGES, TERMINAL_NODES, pipeline_stage_for_phase


StopPredicate = Callable[[], bool]
StateSyncCallback = Callable[[TaskState], None]
TransitionObserver = Callable[[TaskState, str, Event, Transition], None]


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
        *,
        rules: list[Rule] = RULES,
        journal: PipelineJournal | None = None,
        session_store=None,
        stop_requested: StopPredicate | None = None,
        state_sync: StateSyncCallback | None = None,
        transition_observer: TransitionObserver | None = None,
    ) -> None:
        self.registry = registry
        self.persistence = persistence
        self.rules = rules
        self.journal = journal or NullJournal()
        self.session_store = session_store
        self._stop_requested = stop_requested or (lambda: False)
        self._state_sync = state_sync
        self._transition_observer = transition_observer

    def run_task(self, task_id: str) -> TaskState:
        state = self.persistence.load(task_id)
        self.journal.task_started(task_id, state.stage)
        while state.stage not in TERMINAL_NODES:
            from_stage = state.stage
            node = self.registry.get(from_stage)
            event = node.run(state)
            trans = evaluate(self.rules, from_stage, event, state)
            self._apply_delta(state, trans.delta)
            self._apply_event_side_effects(state, event)
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
            if self._stop_requested():
                self.journal.stop_requested(task_id, state.stage)
                return state
        self.journal.task_finished(task_id, state.stage)
        return state

    @staticmethod
    def _apply_event_side_effects(state: TaskState, event: Event) -> None:
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
            state.last_report.changed_files = [str(path).strip() for path in files_changed if str(path).strip()]
        elif isinstance(files_changed, int):
            state.last_report.files_changed = files_changed
        tests_added = meta.get("tests_added")
        if isinstance(tests_added, int):
            state.last_report.tests_added = tests_added
        last_report = meta.get("last_report")
        if isinstance(last_report, dict):
            changed_files = last_report.get("changed_files")
            if isinstance(changed_files, list):
                state.last_report.changed_files = [str(path).strip() for path in changed_files if str(path).strip()]
            test_results = last_report.get("test_results")
            if isinstance(test_results, list):
                state.last_report.test_results = [str(item).strip() for item in test_results if str(item).strip()]
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
        if not isinstance(event, (Pass, HookOk)):
            return
        if pipeline_stage_for_phase(from_stage) == pipeline_stage_for_phase(to_stage):
            if from_stage not in {"commit", "merge_resolving"}:
                return
        StateMachineRunner._clear_hook_reject_tracking(state, clear_recovery_invoked=True)

    @staticmethod
    def _clear_hook_reject_tracking(
        state: TaskState,
        *,
        clear_recovery_invoked: bool,
    ) -> None:
        state.consecutive_same_hook_rejects = 0
        state.last_hook_reject_fingerprint = None
        if clear_recovery_invoked:
            state.hook_reject_recovery_invoked = False

    @staticmethod
    def _apply_delta(state: TaskState, delta: StateDelta) -> None:
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

    def _reset_cross_agent_retry_sessions(
        self,
        *,
        task_id: str,
        from_stage: str,
        to_stage: str,
        event: Event,
    ) -> None:
        if self.session_store is None or not isinstance(event, Reject) or event.source != "agent":
            return
        from_agent_stage = pipeline_stage_for_phase(from_stage)
        to_agent_stage = pipeline_stage_for_phase(to_stage)
        if from_agent_stage == to_agent_stage:
            return
        if from_agent_stage not in AGENT_STAGES or to_agent_stage not in AGENT_STAGES:
            return
        self.session_store.clear_node_sessions(task_id, to_agent_stage)
