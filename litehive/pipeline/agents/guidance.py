"""Built-in startup guidance for agent roles.

Workspace config may extend these with additional bullets per role; a
per-workspace ``.litehive/agents/{role}.md`` file overrides both. Special key
``"all"`` applies to every role.
"""

from copy import deepcopy

DEFAULT_STARTUP_GUIDANCE: dict[str, list[str]] = {
    "all": [],
    "recovery": [
        "Your job is to diagnose why the previous agent failed and fix Litehive infrastructure bugs so the next stage retry can succeed.",
        "Do not redo the failed stage's work, do not implement the task itself, and do not submit the failed stage's verdict on the prior agent's behalf.",
        "Start with the failed subagent artifacts: stdout, stderr, transcript, session metadata, exit code, and any `litehive report` attempt or error.",
        "Submit your own recovery verdict describing the Litehive root cause you found, the fix you made, and why the failed stage should be retried.",
    ],
}


def default_startup_guidance() -> dict[str, list[str]]:
    return deepcopy(DEFAULT_STARTUP_GUIDANCE)
