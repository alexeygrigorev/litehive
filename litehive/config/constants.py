"""Validation constants for the config package."""

VALID_POOL_SELECTION_POLICIES = {"fifo", "priority_first", "dependency_aware"}
VALID_ENGINE_NAMES = frozenset({"codex", "opencode", "gemini", "copilot", "claude", "goz"})
VALID_AGENT_STARTUP_GUIDANCE_KEYS = frozenset(
    {"all", "planner", "swe", "qa", "reviewer", "recovery"}
)
VALID_RETRY_ON_FAILURE_KINDS = frozenset({"execution_limit", "timeout", "network", "service"})
VALID_SANDBOX_NETWORK_MODES = frozenset({"none", "bridge", "host"})
VALID_SANDBOX_WORKSPACE_MODES = frozenset({"ro", "rw"})
VALID_SANDBOX_BACKENDS = frozenset({"docker", "bubblewrap"})
VALID_RUNNER_HOOK_POINTS = frozenset(
    {
        "before_grooming",
        "after_grooming",
        "before_implementing",
        "after_implementing",
        "before_testing",
        "after_testing",
        "before_accepting",
        "after_accepting",
        "after_commit",
    }
)
REJECTABLE_HOOK_POINTS = frozenset({"after_implementing", "after_testing", "after_commit"})
