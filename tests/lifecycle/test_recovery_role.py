from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.roles.recovery import RecoveryAgent


def test_recovery_agent_preserves_target_stage_for_resume_and_advance() -> None:
    agent = object.__new__(RecoveryAgent)

    resume = agent._verdict_to_event(
        AgentVerdict(outcome="resume", metadata={"target_stage": "testing"})
    )
    advance = agent._verdict_to_event(
        AgentVerdict(outcome="advance", metadata={"target_stage": "accepting"})
    )

    assert resume.resume == "testing"
    assert advance.resume == "accepting"


def test_recovery_agent_fails_resume_without_target_stage() -> None:
    agent = object.__new__(RecoveryAgent)

    event = agent._verdict_to_event(AgentVerdict(outcome="resume", metadata={}))

    assert event.reason == "recovery resume verdict missing target_stage"
