from pathlib import Path

from heru.base import CLIExecutionResult

from litehive.agents.session import SubagentInactivityTimeoutPolicy
from litehive.config.model import LitehiveConfig


def test_subagent_inactivity_policy_models_opencode_timeout_exception() -> None:
    config = LitehiveConfig(subagent_inactivity_timeout_seconds=123.0)
    policy = SubagentInactivityTimeoutPolicy(config)

    assert policy.live_timeout_seconds("codex") == 123.0
    assert policy.live_timeout_seconds("opencode") == 300.0


def test_subagent_inactivity_policy_parses_completed_timeout_marker() -> None:
    policy = SubagentInactivityTimeoutPolicy(LitehiveConfig())
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=Path("/tmp"),
        exit_code=0,
        stdout="partial",
        stderr="[litehive] Process killed after 42.5s of inactivity.",
        pid=4242,
    )

    timeout = policy.completed_timeout(execution)

    assert timeout is not None
    assert timeout.idle_seconds == 42.5
    assert timeout.limit_seconds == 42.5
    assert timeout.execution is execution


def test_subagent_inactivity_policy_ignores_regular_stderr() -> None:
    policy = SubagentInactivityTimeoutPolicy(LitehiveConfig())
    execution = CLIExecutionResult(
        adapter="codex",
        argv=("codex", "exec"),
        cwd=Path("/tmp"),
        exit_code=0,
        stdout="done",
        stderr="ordinary warning",
        pid=4242,
    )

    assert policy.completed_timeout(execution) is None
