"""Workspace configuration: validation constants, supporting dataclasses,
and the primary ``LitehiveConfig`` aggregate."""

from dataclasses import dataclass, field


# --- validation constants ---

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

RUNNER_HOOK_EXECUTION_MODES = {"run_all", "fail_fast"}


# --- supporting dataclasses ---


@dataclass(slots=True)
class SandboxCredentialInput:
    env_var: str
    mount_path: str


@dataclass(slots=True)
class ExternalEngineSandboxPolicy:
    enabled: bool = False
    network_mode: str | None = None
    workspace_mode: str | None = None
    environment: list[str] = field(default_factory=list)
    credential_inputs: list[SandboxCredentialInput] = field(default_factory=list)
    extra_ro_binds: list[str] = field(default_factory=list)
    # Writable bind mounts (bwrap --bind). Required when the engine's CLI
    # writes to a fixed host path outside the workspace — e.g. goz writes
    # session files to ~/.goz/sessions.
    extra_rw_binds: list[str] = field(default_factory=list)
    # Hardcoded env vars to set inside the sandbox. Unlike `environment`,
    # which propagates values from the caller, these are fixed values
    # baked into the policy (e.g. CODEX_HOME -> /home/<user>/.codex so
    # codex can find auth.json when HOME is the workspace root).
    setenv: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ExternalEngineSandboxConfig:
    enabled: bool = False
    backend: str = "docker"
    runtime_binary: str = "docker"
    image: str = "litehive-external-engine:latest"
    workspace_mount_path: str = "/workspace"
    binary_mount_root: str = "/litehive/bin"
    runtime_args: list[str] = field(default_factory=list)
    default_network_mode: str = "none"
    default_workspace_mode: str = "rw"
    read_only_rootfs: bool = True
    drop_capabilities: bool = True
    no_new_privileges: bool = True
    tmpfs: list[str] = field(default_factory=lambda: ["/tmp"])
    engine_policies: dict[str, ExternalEngineSandboxPolicy] = field(default_factory=dict)


@dataclass(slots=True)
class RunnerHookConfig:
    command: str
    reject_on_failure: bool = False
    description: str | None = None
    timeout_seconds: float | None = None
    instructions_on_failure: str | None = None


# --- primary config ---


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
    retry_on: list[str] = field(default_factory=lambda: ["execution_limit", "timeout"])
    default_stage_retry_limit: int = 2
    pool_stop_on_failure: bool = False
    pool_max_tasks: int | None = None
    pool_stop_on_dirty_git: bool = False
    pool_stop_on_attention: bool = False
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
        from litehive.config.normalization import (
            normalize_agent_startup_guidance,
            normalize_engine_sequence,
            normalize_external_engine_sandbox_config,
            normalize_retry_on,
            normalize_runner_hooks,
        )

        self.engine_freeze = {str(k): str(v) for k, v in self.engine_freeze.items()}
        self.engine_preference = normalize_engine_sequence(
            list(self.engine_preference),
            field_name="engine_preference",
        )
        self.agent_startup_guidance = normalize_agent_startup_guidance(self.agent_startup_guidance)
        self.retry_on = normalize_retry_on(self.retry_on)
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
