"""Invariant + happy-path tests for the pipeline v2 state machine.

These only exercise the pure rule layer via ``evaluate`` — no engines, no
runner, no persistence.
"""

import pytest

from litehive.domain.recovery import (
    FailureFingerprint,
    RecoveryDisposition,
    RecoveryOutcome,
    RecoveryTrigger,
    TriggerEventKind,
)
from litehive.lifecycle.rules import RULES
from litehive.lifecycle.transitions import evaluate, list_transitions
from litehive.lifecycle.events import (
    Blocked,
    CleanState,
    Crash,
    HookOk,
    NeedsPreExecRecovery,
    Pass,
    PreExecRecoveryFailed,
    PreExecRecoverySucceeded,
    RecoveryFailed,
    RecoverySucceeded,
    Reject,
    Timeout,
)
from litehive.lifecycle.persistence import LastReport, Limits, TaskState
from litehive.lifecycle.transitions import NoTransitionError
from litehive.lifecycle.types import (
    ANY_STAGE_PHASE,
    STAGES,
    TERMINAL_NODES,
    PipelineMode,
)


def make_state(
    stage: str,
    *,
    mode: PipelineMode = PipelineMode.FULL,
    stage_retry: dict[str, int] | None = None,
    pre_exec_recovery_attempt: int = 0,
    files_changed: int = 1,
    tests_added: int = 0,
    stage_retry_limit: int = 3,
    active_recovery_trigger: RecoveryTrigger | None = None,
    recovery_history: list[RecoveryOutcome] | None = None,
) -> TaskState:
    return TaskState(
        task_id="T-0001",
        stage=stage,
        pipeline_mode=mode,
        stage_retry=stage_retry or {},
        active_recovery_trigger=active_recovery_trigger,
        recovery_history=list(recovery_history or []),
        pre_exec_recovery_attempt=pre_exec_recovery_attempt,
        last_report=LastReport(files_changed=files_changed, tests_added=tests_added),
        limits=Limits(stage_retry_limit=stage_retry_limit),
    )


def step(stage: str, event, state: TaskState):
    return evaluate(RULES, stage, event, state)


# ── happy path ────────────────────────────────────────────────────────────


FULL_HAPPY_PATH = [
    ("ready", CleanState(), "worktree_sync"),
    ("worktree_sync", Pass(), "before_grooming"),
    ("before_grooming", HookOk(), "grooming"),
    ("grooming", Pass(), "after_grooming"),
    ("after_grooming", HookOk(), "before_implementing"),
    ("before_implementing", HookOk(), "implementing"),
    ("implementing", Pass(), "after_implementing"),
    ("after_implementing", HookOk(), "before_testing"),
    ("before_testing", HookOk(), "testing"),
    ("testing", Pass(), "after_testing"),
    ("after_testing", HookOk(), "before_accepting"),
    ("before_accepting", HookOk(), "accepting"),
    ("accepting", Pass(), "after_accepting"),
    ("after_accepting", HookOk(), "before_commit"),
    ("before_commit", HookOk(), "commit"),
    ("commit", Pass(), "after_commit"),
    ("after_commit", HookOk(), "done"),
]


@pytest.mark.parametrize("stage,event,expected", FULL_HAPPY_PATH)
def test_full_mode_happy_path(stage, event, expected):
    state = make_state(stage, mode=PipelineMode.FULL)
    assert step(stage, event, state).next == expected


def test_single_mode_entry_skips_grooming_testing_accepting():
    state = make_state("ready", mode=PipelineMode.SINGLE)
    # ready now routes through worktree_sync; from there Pass → single mode
    # entry hits before_implementing.
    assert step("ready", CleanState(), state).next == "worktree_sync"
    sync_state = make_state("worktree_sync", mode=PipelineMode.SINGLE)
    assert step("worktree_sync", Pass(), sync_state).next == "before_implementing"


def test_single_mode_after_implementing_goes_to_commit():
    state = make_state("after_implementing", mode=PipelineMode.SINGLE, files_changed=5)
    assert step("after_implementing", HookOk(), state).next == "before_commit"


def test_single_mode_zero_change_shortcut_to_done():
    state = make_state(
        "after_implementing",
        mode=PipelineMode.SINGLE,
        files_changed=0,
        tests_added=0,
    )
    assert step("after_implementing", HookOk(), state).next == "done"


def test_full_mode_does_not_use_zero_change_shortcut():
    state = make_state(
        "after_implementing",
        mode=PipelineMode.FULL,
        files_changed=0,
        tests_added=0,
    )
    assert step("after_implementing", HookOk(), state).next == "before_testing"


# ── rejections ────────────────────────────────────────────────────────────


def test_grooming_reject_goes_straight_to_recovering():
    state = make_state("grooming")
    trans = step("grooming", Reject(source="agent", reason="x"), state)
    assert trans.next == "recovering"
    assert trans.delta.set_active_recovery_trigger is not None
    assert trans.delta.set_active_recovery_trigger.origin_stage == "grooming"


def test_implementing_reject_retries_with_counter_bump():
    state = make_state("implementing", stage_retry={"implementing": 0})
    trans = step("implementing", Reject(source="agent", reason="x"), state)
    assert trans.next == "implementing"
    assert trans.delta.inc_stage_retry == "implementing"


def test_implementing_reject_routes_to_recovering_when_exhausted():
    state = make_state("implementing", stage_retry={"implementing": 3})
    trans = step("implementing", Reject(source="agent", reason="x"), state)
    assert trans.next == "recovering"


def test_testing_reject_routes_back_to_implementing():
    state = make_state("testing", stage_retry={"testing": 0})
    trans = step("testing", Reject(source="agent", reason="x"), state)
    assert trans.next == "implementing"
    assert trans.delta.inc_stage_retry == "testing"


def test_commit_reject_goes_to_recovering():
    state = make_state("commit")
    trans = step("commit", Reject(source="system", reason="merge conflict"), state)
    assert trans.next == "recovering"


# ── blocked ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage", ["grooming", "implementing", "testing", "accepting"])
def test_blocked_routes_to_recovering(stage):
    state = make_state(stage)
    assert step(stage, Blocked(reason="x"), state).next == "recovering"


# ── tier-3 crash / timeout wildcards ──────────────────────────────────────


@pytest.mark.parametrize("phase", list(ANY_STAGE_PHASE))
def test_crash_in_any_stage_phase_routes_to_recovering(phase):
    state = make_state(phase)
    assert step(phase, Crash(exc_type="E", message="m"), state).next == "recovering"


@pytest.mark.parametrize("phase", list(ANY_STAGE_PHASE))
def test_timeout_in_any_stage_phase_routes_to_recovering(phase):
    state = make_state(phase)
    assert step(phase, Timeout(), state).next == "recovering"


def test_same_stage_same_fingerprint_crash_budget_routes_directly_to_failed():
    prior_trigger = RecoveryTrigger(
        origin_stage="implementing",
        trigger_event_kind=TriggerEventKind.CRASH,
        failure_fingerprint=FailureFingerprint(
            fingerprint="RuntimeError:boom",
            classification="RuntimeError",
        ),
        message="boom",
    )
    state = make_state(
        "implementing",
        recovery_history=[
            RecoveryOutcome(
                trigger=prior_trigger,
                recovery_verdict="resume",
                disposition=RecoveryDisposition.RESUMED,
            )
        ],
    )

    trans = step("implementing", Crash(exc_type="RuntimeError", message="boom"), state)

    assert trans.next == "failed"
    assert trans.delta.failed_reason == "recovery_budget_hit"
    assert trans.delta.set_recovery_failure_explanation is not None


def test_different_stage_crash_gets_fresh_recovery_budget():
    prior_trigger = RecoveryTrigger(
        origin_stage="implementing",
        trigger_event_kind=TriggerEventKind.CRASH,
        failure_fingerprint=FailureFingerprint(
            fingerprint="RuntimeError:boom",
            classification="RuntimeError",
        ),
        message="boom",
    )
    state = make_state(
        "testing",
        recovery_history=[
            RecoveryOutcome(
                trigger=prior_trigger,
                recovery_verdict="resume",
                disposition=RecoveryDisposition.RESUMED,
            )
        ],
    )

    trans = step("testing", Crash(exc_type="RuntimeError", message="boom"), state)

    assert trans.next == "recovering"
    assert trans.delta.set_active_recovery_trigger is not None
    assert trans.delta.set_active_recovery_trigger.origin_stage == "testing"


# ── recovering exit ───────────────────────────────────────────────────────


def test_recovery_succeeded_resume_origin_stage_name():
    state = make_state(
        "recovering",
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="testing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="RuntimeError:boom",
                classification="RuntimeError",
            ),
            message="boom",
        ),
    )
    trans = step("recovering", RecoverySucceeded(resume="testing"), state)
    assert trans.next == "before_testing"
    assert trans.delta.clear_active_recovery_trigger is True


def test_recovery_succeeded_without_destination_fails_explicitly():
    state = make_state(
        "recovering",
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="testing",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="RuntimeError:boom",
                classification="RuntimeError",
            ),
            message="boom",
        ),
    )
    trans = step("recovering", RecoverySucceeded(resume=""), state)
    assert trans.next == "failed"
    assert trans.delta.failed_reason == "recovery_missing_target_stage"


def test_recovery_succeeded_resume_done():
    state = make_state(
        "recovering",
        active_recovery_trigger=RecoveryTrigger(
            origin_stage="commit",
            trigger_event_kind=TriggerEventKind.CRASH,
            failure_fingerprint=FailureFingerprint(
                fingerprint="RuntimeError:boom",
                classification="RuntimeError",
            ),
            message="boom",
        ),
    )
    assert step("recovering", RecoverySucceeded(resume="done"), state).next == "done"


def test_recovery_failed_goes_to_failed_terminal_with_reason():
    trigger = RecoveryTrigger(
        origin_stage="implementing",
        trigger_event_kind=TriggerEventKind.CRASH,
        failure_fingerprint=FailureFingerprint(
            fingerprint="RuntimeError:boom",
            classification="RuntimeError",
        ),
        message="boom",
    )
    state = make_state("recovering", active_recovery_trigger=trigger)
    trans = step("recovering", RecoveryFailed(reason="x"), state)
    assert trans.next == "failed"
    assert trans.delta.failed_reason == "recovery_exhausted"
    assert trans.delta.set_recovery_failure_explanation is not None


def test_recovery_crash_routes_to_failed_with_recovery_crashed():
    trigger = RecoveryTrigger(
        origin_stage="implementing",
        trigger_event_kind=TriggerEventKind.CRASH,
        failure_fingerprint=FailureFingerprint(
            fingerprint="RuntimeError:boom",
            classification="RuntimeError",
        ),
        message="boom",
    )
    state = make_state("recovering", active_recovery_trigger=trigger)
    trans = step("recovering", Crash(exc_type="E", message="boom"), state)
    assert trans.next == "failed"
    assert trans.delta.failed_reason == "recovery_crashed"
    assert "crashed" in (trans.delta.set_recovery_failure_explanation or "")


def test_recovering_wildcard_does_not_route_recovering_back_to_itself():
    """Specific recovering-rule must beat the wildcard Crash→recovering."""
    state = make_state("recovering")
    assert step("recovering", Timeout(), state).next == "failed"


# ── pre-exec recovery ─────────────────────────────────────────────────────


def test_ready_needs_pre_exec_routes_to_pre_exec_node():
    state = make_state("ready")
    trans = step("ready", NeedsPreExecRecovery(), state)
    assert trans.next == "recovering_pre_exec"
    assert trans.delta.inc_pre_exec_recovery_attempt is True


def test_pre_exec_success_resumes_origin_stage():
    state = make_state("recovering_pre_exec")
    trans = step(
        "recovering_pre_exec",
        PreExecRecoverySucceeded(resume_stage="implementing"),
        state,
    )
    assert trans.next == "before_implementing"


def test_pre_exec_failed_goes_to_failed_terminal():
    state = make_state("recovering_pre_exec", pre_exec_recovery_attempt=1)
    trans = step("recovering_pre_exec", PreExecRecoveryFailed(reason="x"), state)
    assert trans.next == "failed"
    assert trans.delta.failed_reason == "pre_exec_recovery_failed"


# ── invariants over the whole rule set ───────────────────────────────────


def test_no_rule_leaves_recovering_looping_to_recovering():
    for rule in list_transitions():
        if rule.from_state == "recovering" and not callable(rule.transition_to):
            assert rule.transition_to != "recovering", f"self-loop on recovering: {rule.description}"


def test_every_stage_phase_has_crash_route():
    for phase in ANY_STAGE_PHASE:
        state = make_state(phase)
        assert step(phase, Crash(exc_type="E", message="m"), state).next == "recovering"


def test_unmatched_event_raises_no_transition():
    state = make_state("grooming")
    with pytest.raises(NoTransitionError):
        step("grooming", HookOk(), state)


def test_terminal_nodes_have_no_outgoing_rules():
    for rule in list_transitions():
        from_nodes = rule.from_state if isinstance(rule.from_state, frozenset) else {rule.from_state}
        assert not (from_nodes & TERMINAL_NODES), f"terminal outgoing: {rule.description}"


def test_all_stages_referenced_in_rules():
    refs: set[str] = set()
    for rule in list_transitions():
        if isinstance(rule.from_state, frozenset):
            refs.update(rule.from_state)
        else:
            refs.add(rule.from_state)
    for stage in STAGES:
        assert f"before_{stage}" in refs
        assert stage in refs
        assert f"after_{stage}" in refs
