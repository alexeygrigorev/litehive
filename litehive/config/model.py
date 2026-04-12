"""Primary workspace config dataclass."""

from dataclasses import dataclass, field

from litehive.config.dataclasses import (
    RUNNER_HOOK_EXECUTION_MODES,
    ExecutionRetryPolicy,
    ExternalEngineSandboxConfig,
    RunnerHookConfig,
)
from litehive.config.normalization import (
    normalize_agent_startup_guidance,
    normalize_engine_sequence,
    normalize_execution_retry_policies,
    normalize_external_engine_sandbox_config,
    normalize_runner_hooks,
)
from litehive.config.retry import default_execution_retry_policies


@dataclass(slots=True)
class LitehiveConfig:
    """Workspace-level configuration for Litehive."""

    default_engine: str = "codex"
    recovery_engine: str | None = None
    litehive_source_path: str | None = None
    process_profile: str = "generic"
    codex_model: str | None = None
    opencode_model: str = "zai-coding-plan/glm-5.1"
    goz_model: str = "glm-5-turbo"
    gemini_model: str | None = None
    copilot_model: str | None = None
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_turns: int = 100
    default_retry_limit: int = 3
    default_stage_retry_limit: int = 2
    execution_retry_policies: dict[str, ExecutionRetryPolicy] = field(
        default_factory=default_execution_retry_policies
    )
    pool_stop_on_failure: bool = False
    pool_max_tasks: int | None = None
    pool_stop_on_dirty_git: bool = False
    pool_selection_policy: str = "dependency_aware"
    runner_hook_execution_mode: str = "run_all"
    runner_hooks: dict[str, list[RunnerHookConfig]] = field(default_factory=dict)
    subagent_inactivity_timeout_seconds: float = 360.0
    inactivity_timeout_seconds: float | None = None
    external_engine_sandbox: ExternalEngineSandboxConfig = field(
        default_factory=ExternalEngineSandboxConfig
    )
    engine_freeze: dict[str, str] = field(default_factory=dict)
    engine_preference: list[str] = field(
        default_factory=lambda: ["codex", "opencode", "gemini", "copilot", "goz"]
    )
    agent_startup_guidance: dict[str, list[str]] = field(default_factory=dict)
    auto_commit: bool = True
    task_mode_name: str = "tasks"
    implementation_mode_name: str = "implementation"

    def __post_init__(self) -> None:
        self.engine_freeze = {str(k): str(v) for k, v in self.engine_freeze.items()}
        self.engine_preference = normalize_engine_sequence(
            list(self.engine_preference),
            field_name="engine_preference",
        )
        self.agent_startup_guidance = normalize_agent_startup_guidance(self.agent_startup_guidance)
        self.execution_retry_policies = normalize_execution_retry_policies(
            self.execution_retry_policies
        )
        self.runner_hook_execution_mode = str(self.runner_hook_execution_mode).strip().lower()
        if self.runner_hook_execution_mode not in RUNNER_HOOK_EXECUTION_MODES:
            allowed = ", ".join(sorted(RUNNER_HOOK_EXECUTION_MODES))
            raise ValueError(
                f"runner_hook_execution_mode must be one of: {allowed}"
            )
        self.runner_hooks = normalize_runner_hooks(self.runner_hooks)
        self.subagent_inactivity_timeout_seconds = float(self.subagent_inactivity_timeout_seconds)
        if self.subagent_inactivity_timeout_seconds <= 0:
            raise ValueError("subagent_inactivity_timeout_seconds must be greater than 0")
        if self.inactivity_timeout_seconds is not None:
            self.inactivity_timeout_seconds = float(self.inactivity_timeout_seconds)
            if self.inactivity_timeout_seconds <= 0:
                raise ValueError("inactivity_timeout_seconds must be greater than 0 when set")
        if self.litehive_source_path is not None:
            self.litehive_source_path = self.litehive_source_path.strip() or None
        self.external_engine_sandbox = normalize_external_engine_sandbox_config(
            self.external_engine_sandbox
        )
