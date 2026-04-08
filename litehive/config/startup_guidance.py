"""Built-in agent startup guidance defaults."""

from copy import deepcopy

DEFAULT_AGENT_STARTUP_GUIDANCE: dict[str, list[str]] = {
    "recovery": [
        "Your job is to diagnose why the previous agent failed and fix Litehive infrastructure bugs so the next stage retry can succeed.",
        "Do not redo the failed stage's work, do not implement the task itself, and do not submit the failed stage's verdict on the prior agent's behalf.",
        "Start with the failed subagent artifacts: stdout, stderr, transcript, session metadata, exit code, and any `litehive report` attempt or error.",
        "Submit your own recovery verdict describing the Litehive root cause you found, the fix you made, and why the failed stage should be retried.",
    ]
}


def default_agent_startup_guidance() -> dict[str, list[str]]:
    """Return built-in startup guidance that supplements workspace config."""
    return deepcopy(DEFAULT_AGENT_STARTUP_GUIDANCE)
