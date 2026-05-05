from litehive.lifecycle.events import RecoveryFailed, RecoverySucceeded
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.roles.recovery import RecoveryAgent


def test_recovery_agent_preserves_target_stage_for_resume_and_advance() -> None:
    agent = object.__new__(RecoveryAgent)

    resume = agent.verdict_to_event(AgentVerdict(outcome="resume", metadata={"target_stage": "testing"}))
    advance = agent.verdict_to_event(AgentVerdict(outcome="advance", metadata={"target_stage": "accepting"}))

    assert isinstance(resume, RecoverySucceeded)
    assert isinstance(advance, RecoverySucceeded)
    assert resume.resume == "testing"
    assert advance.resume == "accepting"


def test_recovery_agent_fails_resume_without_target_stage() -> None:
    agent = object.__new__(RecoveryAgent)

    event = agent.verdict_to_event(AgentVerdict(outcome="resume", metadata={}))

    assert isinstance(event, RecoveryFailed)
    assert event.reason == "recovery resume verdict missing target_stage"
