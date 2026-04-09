"""Validation constants for the config package."""

VALID_POOL_SELECTION_POLICIES = {"fifo", "priority_first", "dependency_aware"}
VALID_ENGINE_NAMES = frozenset({"codex", "opencode", "gemini", "copilot", "claude", "goz"})
VALID_AGENT_STARTUP_GUIDANCE_KEYS = frozenset(
    {"all", "planner", "swe", "qa", "reviewer", "recovery"}
)
VALID_EXECUTION_RETRY_SELECTORS = frozenset({*VALID_ENGINE_NAMES, "external_cli"})
VALID_EXECUTION_RETRY_CLASSIFICATIONS = frozenset({"timeout", "network", "service"})
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
REJECTABLE_HOOK_POINTS = frozenset({"after_implementing", "after_testing"})
_LEGACY_HOOK_POINT_MAP = {
    "before_swe_implementation": "before_implementing",
    "after_swe_implementation": "after_implementing",
    "before_pm_acceptance": "before_accepting",
    "after_pm_acceptance": "after_accepting",
    "after_merge": "after_commit",
}
MODEL_FAMILY_RETRY_SELECTOR_PREFIX = "model_family:"
ENGINE_CATEGORY_RETRY_SELECTOR_PREFIX = "engine_category:"
