"""Built-in startup guidance for agent roles.

Workspace config may extend these with additional bullets per role; a
per-workspace ``.litehive/agents/{role}.md`` file overrides both. Special key
``"all"`` applies to every role.
"""

from copy import deepcopy

DEFAULT_STARTUP_GUIDANCE: dict[str, list[str]] = {
    "all": [],
    "qa": [
        "Start from the latest implementing report and the repo's documented verification flow before broad test sweeps.",
        "Use the default deterministic test suite and targeted checks first; only gate on opt-in, external-boundary, or authenticated integration coverage when the task contract or changed surface requires it.",
    ],
    "recovery": [
        "Your job is to diagnose why the previous agent failed and fix Litehive infrastructure bugs so the next stage retry can succeed.",
        "Do not redo the failed stage's work, do not implement the task itself, and do not submit the failed stage's verdict on the prior agent's behalf.",
        "Start with `litehive task evidence <task_id>`: DB-backed task state, recovery trigger/history, latest report/activity, subagent outcome, and worktree summary.",
        "Check the failure in this order: did the agent produce output, did it try `litehive agent report`, what exact Litehive error came back, and what Litehive code path caused it.",
        "This recovery turn runs in the Litehive source repo so you can patch Litehive itself, not the task worktree.",
        "Submit your own recovery verdict describing the Litehive root cause you found, the fix you made, and why the failed stage should be retried.",
    ],
}


def default_startup_guidance() -> dict[str, list[str]]:
    return deepcopy(DEFAULT_STARTUP_GUIDANCE)
