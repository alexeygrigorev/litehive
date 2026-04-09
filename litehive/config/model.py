"""Primary workspace config dataclass."""

from dataclasses import dataclass, field

from litehive.config.dataclasses import (
    ExecutionRetryPolicy,
    ExternalEngineSandboxConfig,
    RunnerHookConfig,
    SubagentResourceLimitsConfig,
)
from litehive.config.normalization import (
    _normalize_agent_startup_guidance,
    _normalize_engine_int_map,
    _normalize_engine_sequence,
    _normalize_execution_retry_policies,
    _normalize_external_engine_sandbox_config,
    _normalize_runner_hooks,
    _normalize_subagent_resource_limits,
)
from litehive.config.retry import _default_execution_retry_policies


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
    claude_enabled: bool = False
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_turns: int = 100
    pool_usage_cap: int | None = None
    pool_cost_cap: int | None = None
    engine_usage_caps: dict[str, int] = field(default_factory=dict)
    engine_budget_caps: dict[str, int] = field(default_factory=dict)
    engine_costs: dict[str, int] = field(
        default_factory=lambda: {
            "codex": 1,
            "opencode": 1,
            "gemini": 1,
            "copilot": 1,
            "claude": 3,
            "goz": 1,
        }
    )
    default_retry_limit: int = 3
    default_stage_retry_limit: int = 2
    execution_retry_policies: dict[str, ExecutionRetryPolicy] = field(
        default_factory=_default_execution_retry_policies
    )
    pool_stop_on_failure: bool = False
    pool_max_tasks: int | None = None
    pool_stop_on_execution_limit: bool = False
    pool_quota_threshold: int | None = None
    pool_budget_threshold: int | None = None
    pool_stop_on_dirty_git: bool = False
    pool_selection_policy: str = "dependency_aware"
    runner_hooks: dict[str, list[RunnerHookConfig]] = field(default_factory=dict)
    subagent_inactivity_timeout_seconds: float = 360.0
    inactivity_timeout_seconds: float | None = None
    subagent_resource_limits: SubagentResourceLimitsConfig = field(
        default_factory=SubagentResourceLimitsConfig
    )
    external_engine_sandbox: ExternalEngineSandboxConfig = field(
        default_factory=ExternalEngineSandboxConfig
    )
    engine_freeze: dict[str, str] = field(default_factory=dict)
    engine_preference: list[str] = field(
        default_factory=lambda: ["codex", "opencode", "gemini", "copilot", "goz"]
    )
    agent_startup_guidance: dict[str, list[str]] = field(default_factory=dict)
    parallel_capacity: int = 1
    parallel_integration_check: str | None = None
    auto_commit: bool = True
    task_mode_name: str = "tasks"
    implementation_mode_name: str = "implementation"

    def __post_init__(self) -> None:
        self.engine_usage_caps = _normalize_engine_int_map(
            self.engine_usage_caps,
            field_name="engine_usage_caps",
        )
        self.engine_budget_caps = _normalize_engine_int_map(
            self.engine_budget_caps,
            field_name="engine_budget_caps",
        )
        self.engine_costs = _normalize_engine_int_map(
            self.engine_costs,
            field_name="engine_costs",
        )
        self.engine_freeze = {str(k): str(v) for k, v in self.engine_freeze.items()}
        self.engine_preference = _normalize_engine_sequence(
            list(self.engine_preference),
            field_name="engine_preference",
        )
        self.agent_startup_guidance = _normalize_agent_startup_guidance(self.agent_startup_guidance)
        self.execution_retry_policies = _normalize_execution_retry_policies(
            self.execution_retry_policies
        )
        self.runner_hooks = _normalize_runner_hooks(self.runner_hooks)
        self.subagent_inactivity_timeout_seconds = float(self.subagent_inactivity_timeout_seconds)
        if self.subagent_inactivity_timeout_seconds <= 0:
            raise ValueError("subagent_inactivity_timeout_seconds must be greater than 0")
        if self.inactivity_timeout_seconds is not None:
            self.inactivity_timeout_seconds = float(self.inactivity_timeout_seconds)
            if self.inactivity_timeout_seconds <= 0:
                raise ValueError("inactivity_timeout_seconds must be greater than 0 when set")
        if self.litehive_source_path is not None:
            self.litehive_source_path = self.litehive_source_path.strip() or None
        self.subagent_resource_limits = _normalize_subagent_resource_limits(
            self.subagent_resource_limits,
            process_profile=self.process_profile,
        )
        self.external_engine_sandbox = _normalize_external_engine_sandbox_config(
            self.external_engine_sandbox
        )
        self.parallel_capacity = int(self.parallel_capacity)
        if self.parallel_capacity < 1:
            raise ValueError("parallel_capacity must be at least 1")
