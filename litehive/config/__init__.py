"""Workspace configuration, process profiles, and bootstrap helpers.

This package splits the configuration into focused sub-modules:

- ``constants``: validation sets, prefixes, and default routing keys.
- ``dataclasses``: supporting dataclasses (retry policy, sandbox, hooks, etc.).
- ``normalization``: validation and normalization helpers.
- ``retry``: default execution retry policy factory with a single shared definition.
- ``profiles``: process profile definitions and rendering.

The main :class:`LitehiveConfig` dataclass lives here and re-exports
everything that external modules import from ``litehive.config``.
"""

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from litehive.config.constants import (  # noqa: F401
    ENGINE_CATEGORY_RETRY_SELECTOR_PREFIX,
    MODEL_FAMILY_RETRY_SELECTOR_PREFIX,
    VALID_AGENT_STARTUP_GUIDANCE_KEYS,
    VALID_ENGINE_NAMES,
    VALID_EXECUTION_RETRY_CLASSIFICATIONS,
    VALID_EXECUTION_RETRY_SELECTORS,
    VALID_POOL_SELECTION_POLICIES,
    VALID_RUNNER_HOOK_POINTS,
    VALID_SANDBOX_BACKENDS,
    VALID_SANDBOX_NETWORK_MODES,
    VALID_SANDBOX_WORKSPACE_MODES,
    VALID_TASK_ROUTING_KEYS,
    _default_task_engine_routing,
)
from litehive.config.dataclasses import (  # noqa: F401
    ExecutionRetryPolicy,
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    RunnerHookConfig,
    SandboxCredentialInput,
    SubagentResourceLimitsConfig,
)
from litehive.config.normalization import (  # noqa: F401
    _normalize_agent_startup_guidance,
    _normalize_engine_int_map,
    _normalize_engine_sequence,
    _normalize_execution_retry_policies,
    _normalize_external_engine_sandbox_config,
    _normalize_runner_hooks,
    _normalize_subagent_resource_limits,
    normalize_task_engine_routing,
)
from litehive.config.profiles import (  # noqa: F401
    PROCESS_PROFILES,
    SHARED_PROCESS_PROFILE,
    available_process_profiles,
    render_context_template,
    resolve_process_profile,
)
from litehive.config.retry import _default_execution_retry_policies  # noqa: F401


@dataclass(slots=True)
class LitehiveConfig:
    """Workspace-level configuration for Litehive.

    Fields:
        default_engine: Engine used when no task-level or routing-level
            override applies. One of ``codex``, ``opencode``, ``gemini``,
            ``copilot``, ``claude``, ``goz``.
        recovery_engine: Optional separate engine for recovery and
            merge-resolution agents. Falls back to ``default_engine``
            when unset.
        litehive_source_path: Absolute path to the Litehive source tree
            for filing upstream follow-up issues.
        process_profile: Process profile name (e.g. ``generic``,
            ``python``, ``rust``). Controls context template rendering.
        codex_model: Optional model override for the Codex engine.
        opencode_model: Default model for the OpenCode engine.
        goz_model: Default model for the Goz engine.
        gemini_model: Optional model override for the Gemini engine.
        copilot_model: Optional model override for the Copilot engine.
        claude_enabled: Whether the Claude engine is opted-in for this
            workspace. Claude requires explicit enablement.
        claude_model: Default model for the Claude engine.
        claude_max_turns: Maximum conversation turns for a single Claude
            session.
        pool_usage_cap: Maximum number of engine invocations allowed
            across the pool before triggering a stop condition.
        pool_cost_cap: Maximum cumulative engine cost budget before
            triggering a stop condition.
        engine_usage_caps: Per-engine invocation caps (e.g.
            ``{"codex": 5}``).
        engine_budget_caps: Per-engine cost caps. When an engine's
            cumulative cost exceeds its cap, the pool stops or falls back.
        engine_costs: Per-engine cost multipliers. Each engine invocation
            adds its cost to the running pool budget ledger.
        default_retry_limit: Maximum whole-task retries before the task
            is marked as failed.
        default_stage_retry_limit: Maximum retries within a single stage
            before the task is marked as failed.
        execution_retry_policies: Per-engine and per-selector retry
            policies governing automatic backoff and retry for transient
            failures such as timeouts, network errors, or service
            outages.
        pool_stop_on_failure: Whether the pool stops immediately when a
            task fails.
        pool_max_tasks: Maximum number of tasks the pool will run in a
            single drain cycle before stopping.
        pool_stop_on_execution_limit: Whether the pool stops when any
            engine hits its usage or budget cap.
        pool_quota_threshold: Usage-cap threshold that triggers a
            stop condition when the remaining quota falls below this
            value.
        pool_budget_threshold: Budget-cap threshold that triggers a
            stop condition when the remaining budget falls below this
            value.
        pool_stop_on_dirty_git: Whether the pool stops when the git
            working tree has uncommitted changes.
        pool_selection_policy: Strategy for selecting the next task from
            the queue. One of ``fifo``, ``priority_first``,
            ``dependency_aware``.
        pre_acceptance_command: Legacy shell command that runs before
            PM acceptance. Automatically folded into
            ``runner_hooks.before_pm_acceptance`` as a blocking hook.
        runner_hooks: Shell hooks that execute around implementation and
            acceptance boundaries. Keyed by hook point (e.g.
            ``before_swe_implementation``).
        subagent_inactivity_timeout_seconds: Maximum seconds a subagent
            can be idle before it is considered stalled.
        subagent_resource_limits: Resource constraints (memory, CPU,
            process count) applied to subagent execution sandboxes.
        external_engine_sandbox: Container-based sandbox configuration
            for external engine isolation.
        task_engine_routing: Per-task-type engine preference ordering.
            Maps task types (e.g. ``research``, ``bugfix``) to an
            ordered list of engines.
        engine_fallbacks: Ordered fallback engines when the primary
            engine for a task is unavailable due to quota, budget, or
            transient failures.
        agent_startup_guidance: Per-role startup guidance text injected
            into subagent prompts at the beginning of a session.
        auto_commit: Whether accepted tasks automatically proceed to
            ``commit_to_git`` and create a checkpoint commit.
        task_mode_name: Mode name for typed task creation that produces
            richer shaping artifacts (``brief.md``, etc.).
        implementation_mode_name: Mode name for simpler implementation-
            style task intake without extra shaping files.
    """

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
    pre_acceptance_command: str | None = None
    runner_hooks: dict[str, list[RunnerHookConfig]] = field(default_factory=dict)
    subagent_inactivity_timeout_seconds: float = 300.0
    subagent_resource_limits: SubagentResourceLimitsConfig = field(
        default_factory=SubagentResourceLimitsConfig
    )
    external_engine_sandbox: ExternalEngineSandboxConfig = field(
        default_factory=ExternalEngineSandboxConfig
    )
    task_engine_routing: dict[str, list[str]] = field(default_factory=_default_task_engine_routing)
    engine_fallbacks: dict[str, list[str]] = field(
        default_factory=lambda: {
            "codex": ["opencode", "gemini", "copilot"],
            "opencode": ["codex", "gemini", "copilot"],
            "gemini": ["codex", "opencode", "copilot"],
            "copilot": ["codex", "opencode", "gemini"],
            "goz": ["copilot", "codex", "opencode", "gemini"],
        }
    )
    agent_startup_guidance: dict[str, list[str]] = field(default_factory=dict)
    auto_commit: bool = True
    task_mode_name: str = "tasks"
    implementation_mode_name: str = "implementation"

    def __post_init__(self) -> None:
        self.task_engine_routing = normalize_task_engine_routing(self.task_engine_routing)
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
        self.engine_fallbacks = {
            engine_name: _normalize_engine_sequence(
                list(fallbacks),
                field_name=f"engine_fallbacks[{engine_name}]",
            )
            for engine_name, fallbacks in self.engine_fallbacks.items()
        }
        self.agent_startup_guidance = _normalize_agent_startup_guidance(self.agent_startup_guidance)
        self.execution_retry_policies = _normalize_execution_retry_policies(
            self.execution_retry_policies
        )
        self.runner_hooks = _normalize_runner_hooks(self.runner_hooks)
        self.subagent_inactivity_timeout_seconds = float(self.subagent_inactivity_timeout_seconds)
        if self.subagent_inactivity_timeout_seconds <= 0:
            raise ValueError("subagent_inactivity_timeout_seconds must be greater than 0")
        if self.pre_acceptance_command is not None:
            self.pre_acceptance_command = self.pre_acceptance_command.strip() or None
        if self.litehive_source_path is not None:
            self.litehive_source_path = self.litehive_source_path.strip() or None
        if self.pre_acceptance_command:
            before_acceptance_hooks = self.runner_hooks.setdefault("before_pm_acceptance", [])
            matched_legacy_hook = False
            for hook in before_acceptance_hooks:
                if hook.command == self.pre_acceptance_command:
                    hook.blocking = True
                    matched_legacy_hook = True
            if not matched_legacy_hook:
                before_acceptance_hooks.append(
                    RunnerHookConfig(command=self.pre_acceptance_command, blocking=True)
                )
        self.subagent_resource_limits = _normalize_subagent_resource_limits(
            self.subagent_resource_limits,
            process_profile=self.process_profile,
        )
        self.external_engine_sandbox = _normalize_external_engine_sandbox_config(
            self.external_engine_sandbox
        )


def workspace_dir(root: Path) -> Path:
    return root / ".litehive"


def config_path(root: Path) -> Path:
    return workspace_dir(root) / "config.yaml"


def global_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "litehive" / "config.yaml"
    return Path.home() / ".config" / "litehive" / "config.yaml"


def state_path(root: Path) -> Path:
    return workspace_dir(root) / "state.yaml"


def context_path(root: Path) -> Path:
    return workspace_dir(root) / "context.md"


def workspace_gitignore_path(root: Path) -> Path:
    return workspace_dir(root) / ".gitignore"


def render_workspace_gitignore() -> str:
    return "\n".join(
        [
            "# Transient litehive runtime state",
            ".lock",
            ".runner.lock",
            "logs/",
            "pool-summary.txt",
            "engine-monitoring.yaml",
            "worktrees/",
            "tasks/*/runtime.yaml",
            "tasks/*/reports/commit_to_git-*.yaml",
            "",
        ]
    )


def ensure_workspace(root: Path, config: LitehiveConfig | None = None) -> Path:
    root = root.resolve()
    base = workspace_dir(root)
    tasks = base / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)

    cfg = config or LitehiveConfig()
    if not config_path(root).exists():
        initial_config = asdict(cfg) if config is not None else {}
        config_path(root).write_text(
            yaml.safe_dump(initial_config, sort_keys=False),
            encoding="utf-8",
        )

    if not state_path(root).exists():
        state_path(root).write_text(
            yaml.safe_dump(
                {
                    "active_task_id": None,
                    "mode": cfg.implementation_mode_name,
                    "queue": [],
                    "pool_stop_reason": None,
                    "next_task_number": 0,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    if not context_path(root).exists():
        context_path(root).write_text(
            render_context_template(cfg.process_profile), encoding="utf-8"
        )

    if not workspace_gitignore_path(root).exists():
        workspace_gitignore_path(root).write_text(
            render_workspace_gitignore(),
            encoding="utf-8",
        )

    return base


def _read_config_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return dict(data)


def _merge_config_layers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_config_layers(
                dict(merged[key]),
                value,
            )
            continue
        merged[key] = value
    return merged


def load_effective_config_data(root: Path) -> dict[str, Any]:
    default_data = asdict(LitehiveConfig())
    global_data = _read_config_mapping(global_config_path())
    local_data = _read_config_mapping(config_path(root))
    return _merge_config_layers(
        _merge_config_layers(default_data, global_data),
        local_data,
    )


def load_config(root: Path) -> LitehiveConfig:
    ensure_workspace(root)
    data = load_effective_config_data(root)
    if data.get("process_profile") not in PROCESS_PROFILES:
        data["process_profile"] = "generic"
    if data.get("pool_selection_policy") not in VALID_POOL_SELECTION_POLICIES:
        data["pool_selection_policy"] = "dependency_aware"
    return LitehiveConfig(**data)


def load_context(root: Path) -> str:
    ensure_workspace(root)
    return context_path(root).read_text(encoding="utf-8")


def format_external_engine_sandbox(config: LitehiveConfig) -> str:
    sandbox = config.external_engine_sandbox
    if not sandbox.enabled:
        return "disabled"
    policy_parts: list[str] = []
    for engine_name in sorted(sandbox.engine_policies):
        policy = sandbox.engine_policies[engine_name]
        envs = ",".join(policy.environment) or "-"
        creds = ",".join(item.env_var for item in policy.credential_inputs) or "-"
        policy_parts.append(
            f"{engine_name}=enabled:{policy.enabled} net:{policy.network_mode or sandbox.default_network_mode} "
            f"workspace:{policy.workspace_mode or sandbox.default_workspace_mode} env:{envs} creds:{creds}"
        )
    policies = "; ".join(policy_parts) if policy_parts else "no engine policies"
    return (
        f"enabled backend:{sandbox.backend} runtime:{sandbox.runtime_binary} image:{sandbox.image} "
        f"default_net:{sandbox.default_network_mode} default_workspace:{sandbox.default_workspace_mode} "
        f"policies: {policies}"
    )


def format_subagent_resource_limits(config: LitehiveConfig) -> str:
    limits = config.subagent_resource_limits
    if not limits.enabled:
        return "disabled"
    details: list[str] = []
    if limits.memory_mb is not None:
        details.append(f"memory_mb:{limits.memory_mb}")
    if limits.cpu_count is not None:
        details.append(f"cpu_count:{limits.cpu_count:g}")
    if limits.process_limit is not None:
        details.append(f"process_limit:{limits.process_limit}")
    return "enabled " + " ".join(details) if details else "enabled"


def format_runner_hooks(config: LitehiveConfig) -> str:
    if not config.runner_hooks:
        return "none"
    parts: list[str] = []
    for point in sorted(config.runner_hooks):
        hooks = ", ".join(
            f"{'blocking' if hook.blocking else 'non-blocking'}:{hook.command}"
            for hook in config.runner_hooks[point]
        )
        parts.append(f"{point}=[{hooks}]")
    return "; ".join(parts)
