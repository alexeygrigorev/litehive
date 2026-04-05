"""Validation constants and default routing for the config package."""

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
        "before_swe_implementation",
        "after_swe_implementation",
        "before_pm_acceptance",
        "after_pm_acceptance",
    }
)
MODEL_FAMILY_RETRY_SELECTOR_PREFIX = "model_family:"
ENGINE_CATEGORY_RETRY_SELECTOR_PREFIX = "engine_category:"


def _default_task_engine_routing() -> dict[str, list[str]]:
    return {
        "adapter": ["codex", "opencode", "gemini", "copilot", "goz"],
        "bugfix": ["codex", "opencode", "copilot", "gemini", "goz"],
        "research": ["gemini", "codex", "opencode", "copilot", "goz"],
        "review": ["copilot", "codex", "opencode", "gemini", "goz"],
        "refactor": ["opencode", "codex", "copilot", "gemini", "goz"],
        "docs": ["codex", "gemini", "opencode", "copilot", "goz"],
        "intake": ["opencode", "codex", "gemini", "copilot", "goz"],
    }


VALID_TASK_ROUTING_KEYS = frozenset(_default_task_engine_routing())
