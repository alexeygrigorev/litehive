"""Workspace configuration: validation constants, supporting dataclasses,
and the primary ``LitehiveConfig`` aggregate."""

from dataclasses import dataclass, field
import re
from typing import Mapping, Sequence


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
        "after_merge",
    }
)
REJECTABLE_HOOK_POINTS = frozenset(
    {"after_implementing", "after_testing", "after_commit", "after_merge"}
)

RUNNER_HOOK_EXECUTION_MODES = {"run_all", "fail_fast"}
DEFAULT_SUBAGENT_INACTIVITY_TIMEOUT_SECONDS = 300.0


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
    # Writable bind mounts (docker -v :rw). Required when the engine's CLI
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
    reject_on_failure: bool | None = None
    blocking: bool | None = None
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
    subagent_inactivity_timeout_seconds: float = DEFAULT_SUBAGENT_INACTIVITY_TIMEOUT_SECONDS
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


def normalize_engine_sequence(engines: Sequence[str], *, field_name: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for engine_name in engines:
        if engine_name not in VALID_ENGINE_NAMES:
            allowed = ", ".join(sorted(VALID_ENGINE_NAMES))
            raise ValueError(f"{field_name} engine must be one of: {allowed}")
        if engine_name in seen:
            continue
        seen.add(engine_name)
        normalized.append(engine_name)
    return normalized


def normalize_agent_startup_guidance(
    guidance: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    if guidance is None:
        return {}

    normalized: dict[str, list[str]] = {}
    for role_name, entries in guidance.items():
        key = str(role_name).strip().lower()
        if key not in VALID_AGENT_STARTUP_GUIDANCE_KEYS:
            allowed = ", ".join(sorted(VALID_AGENT_STARTUP_GUIDANCE_KEYS))
            raise ValueError(f"agent_startup_guidance keys must be one of: {allowed}")
        cleaned: list[str] = []
        for item in entries:
            text = str(item).strip()
            if text:
                cleaned.append(text)
        if cleaned:
            normalized[key] = cleaned
    return normalized


def normalize_retry_on(
    retry_on: Sequence[str] | None,
    *,
    field_name: str = "retry_on",
) -> list[str]:
    if retry_on is None:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_kind in retry_on:
        kind = str(raw_kind).strip().lower()
        if not kind:
            continue
        if kind not in VALID_RETRY_ON_FAILURE_KINDS:
            allowed = ", ".join(sorted(VALID_RETRY_ON_FAILURE_KINDS))
            raise ValueError(f"{field_name} must contain only: {allowed}")
        if kind in seen:
            continue
        seen.add(kind)
        normalized.append(kind)
    return normalized


def _normalize_runner_hook_config(
    raw_hook: RunnerHookConfig | Mapping[str, object],
    *,
    field_name: str,
    point: str,
) -> RunnerHookConfig:
    hook = raw_hook if isinstance(raw_hook, RunnerHookConfig) else RunnerHookConfig(**dict(raw_hook))
    hook.command = hook.command.strip()
    if not hook.command:
        raise ValueError(f"{field_name}.command must not be empty")
    if hook.reject_on_failure is not None and not isinstance(hook.reject_on_failure, bool):
        raise ValueError(f"{field_name}.reject_on_failure must be a boolean")
    if hook.blocking is not None and not isinstance(hook.blocking, bool):
        raise ValueError(f"{field_name}.blocking must be a boolean")
    if hook.blocking is not None:
        if (
            hook.reject_on_failure is not None
            and hook.reject_on_failure != hook.blocking
        ):
            raise ValueError(
                f"{field_name}.blocking must match {field_name}.reject_on_failure when both are set"
            )
        hook.reject_on_failure = hook.blocking
    if hook.reject_on_failure is None:
        hook.reject_on_failure = False
    if hook.description is not None:
        hook.description = hook.description.strip() or None
    if hook.instructions_on_failure is not None:
        hook.instructions_on_failure = hook.instructions_on_failure.strip() or None
    if hook.timeout_seconds is not None:
        hook.timeout_seconds = float(hook.timeout_seconds)
        if hook.timeout_seconds <= 0:
            raise ValueError(f"{field_name}.timeout_seconds must be greater than 0")
    if hook.reject_on_failure and point not in REJECTABLE_HOOK_POINTS:
        allowed = ", ".join(sorted(REJECTABLE_HOOK_POINTS))
        field_label = "blocking" if hook.blocking is not None else "reject_on_failure"
        raise ValueError(
            f"{field_name}.{field_label} is only valid for: {allowed} (got {point})"
        )
    return hook


def normalize_runner_hooks(
    raw_hooks: Mapping[str, Sequence[RunnerHookConfig | Mapping[str, object]]] | None,
) -> dict[str, list[RunnerHookConfig]]:
    if raw_hooks is None:
        return {}

    normalized: dict[str, list[RunnerHookConfig]] = {}
    for point, hooks in raw_hooks.items():
        if point not in VALID_RUNNER_HOOK_POINTS:
            allowed = ", ".join(sorted(VALID_RUNNER_HOOK_POINTS))
            raise ValueError(f"runner_hooks key must be one of: {allowed}")
        normalized[point] = [
            _normalize_runner_hook_config(
                hook,
                field_name=f"runner_hooks[{point}][{index}]",
                point=point,
            )
            for index, hook in enumerate(hooks)
        ]
    return normalized


def _normalize_sandbox_credential_input(
    raw_input: SandboxCredentialInput | Mapping[str, object],
    *,
    field_name: str,
) -> SandboxCredentialInput:
    if isinstance(raw_input, SandboxCredentialInput):
        credential = raw_input
    else:
        env_var = str(raw_input.get("env_var", "")).strip()
        mount_path = str(raw_input.get("mount_path", "")).strip()
        credential = SandboxCredentialInput(env_var=env_var, mount_path=mount_path)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", credential.env_var):
        raise ValueError(f"{field_name}.env_var must be an uppercase environment variable name")
    if not credential.mount_path.startswith("/"):
        raise ValueError(f"{field_name}.mount_path must be an absolute container path")
    return credential


def _normalize_bind_list(raw_binds: list[str], *, field_name: str) -> list[str]:
    normalized: list[str] = []
    for index, raw_path in enumerate(raw_binds):
        host_path = raw_path.strip()
        if not host_path:
            continue
        if not host_path.startswith("/"):
            raise ValueError(f"{field_name}[{index}] must be an absolute host path")
        normalized.append(host_path)
    return normalized


def _normalize_external_engine_sandbox_policy(
    raw_policy: ExternalEngineSandboxPolicy | Mapping[str, object],
    *,
    field_name: str,
) -> ExternalEngineSandboxPolicy:
    if isinstance(raw_policy, ExternalEngineSandboxPolicy):
        policy = raw_policy
    else:
        policy = ExternalEngineSandboxPolicy(
            enabled=bool(raw_policy.get("enabled", False)),
            network_mode=(
                None
                if raw_policy.get("network_mode") is None
                else str(raw_policy.get("network_mode"))
            ),
            workspace_mode=(
                None
                if raw_policy.get("workspace_mode") is None
                else str(raw_policy.get("workspace_mode"))
            ),
            environment=[str(item) for item in raw_policy.get("environment", [])],
            credential_inputs=[
                _normalize_sandbox_credential_input(
                    item,
                    field_name=f"{field_name}.credential_inputs[{index}]",
                )
                for index, item in enumerate(raw_policy.get("credential_inputs", []))
            ],
            extra_ro_binds=[str(item).strip() for item in raw_policy.get("extra_ro_binds", [])],
            extra_rw_binds=[str(item).strip() for item in raw_policy.get("extra_rw_binds", [])],
            setenv={
                str(key): str(value)
                for key, value in (raw_policy.get("setenv") or {}).items()
            },
        )
    for index, env_name in enumerate(policy.environment):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise ValueError(
                f"{field_name}.environment[{index}] must be an uppercase environment variable name"
            )
    for env_name in policy.setenv.keys():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise ValueError(
                f"{field_name}.setenv key {env_name!r} must be an uppercase environment variable name"
            )
    if policy.network_mode is not None and policy.network_mode not in VALID_SANDBOX_NETWORK_MODES:
        allowed = ", ".join(sorted(VALID_SANDBOX_NETWORK_MODES))
        raise ValueError(f"{field_name}.network_mode must be one of: {allowed}")
    if (
        policy.workspace_mode is not None
        and policy.workspace_mode not in VALID_SANDBOX_WORKSPACE_MODES
    ):
        allowed = ", ".join(sorted(VALID_SANDBOX_WORKSPACE_MODES))
        raise ValueError(f"{field_name}.workspace_mode must be one of: {allowed}")
    policy.extra_ro_binds = _normalize_bind_list(
        policy.extra_ro_binds, field_name=f"{field_name}.extra_ro_binds"
    )
    policy.extra_rw_binds = _normalize_bind_list(
        policy.extra_rw_binds, field_name=f"{field_name}.extra_rw_binds"
    )
    return policy


def normalize_external_engine_sandbox_config(
    raw_config: ExternalEngineSandboxConfig | Mapping[str, object] | None,
) -> ExternalEngineSandboxConfig:
    if raw_config is None:
        return ExternalEngineSandboxConfig()
    if isinstance(raw_config, ExternalEngineSandboxConfig):
        config = raw_config
    else:
        backend = str(raw_config.get("backend", "docker")).strip().lower()
        config = ExternalEngineSandboxConfig(
            enabled=bool(raw_config.get("enabled", False)),
            backend=backend,
            runtime_binary=str(raw_config.get("runtime_binary", "docker")),
            image=str(raw_config.get("image", "litehive-external-engine:latest")),
            workspace_mount_path=str(raw_config.get("workspace_mount_path", "/workspace")),
            binary_mount_root=str(raw_config.get("binary_mount_root", "/litehive/bin")),
            runtime_args=[str(item) for item in raw_config.get("runtime_args", [])],
            default_network_mode=str(raw_config.get("default_network_mode", "none")),
            default_workspace_mode=str(raw_config.get("default_workspace_mode", "rw")),
            read_only_rootfs=bool(raw_config.get("read_only_rootfs", True)),
            drop_capabilities=bool(raw_config.get("drop_capabilities", True)),
            no_new_privileges=bool(raw_config.get("no_new_privileges", True)),
            tmpfs=[str(item) for item in raw_config.get("tmpfs", ["/tmp"])],
            engine_policies={
                engine_name: _normalize_external_engine_sandbox_policy(
                    policy,
                    field_name=f"external_engine_sandbox.engine_policies[{engine_name}]",
                )
                for engine_name, policy in dict(raw_config.get("engine_policies", {})).items()
            },
        )
    if config.backend not in VALID_SANDBOX_BACKENDS:
        allowed = ", ".join(sorted(VALID_SANDBOX_BACKENDS))
        raise ValueError(f"external_engine_sandbox.backend must be one of: {allowed}")
    if config.default_network_mode not in VALID_SANDBOX_NETWORK_MODES:
        allowed = ", ".join(sorted(VALID_SANDBOX_NETWORK_MODES))
        raise ValueError(f"external_engine_sandbox.default_network_mode must be one of: {allowed}")
    if config.default_workspace_mode not in VALID_SANDBOX_WORKSPACE_MODES:
        allowed = ", ".join(sorted(VALID_SANDBOX_WORKSPACE_MODES))
        raise ValueError(
            f"external_engine_sandbox.default_workspace_mode must be one of: {allowed}"
        )
    if not config.workspace_mount_path.startswith("/"):
        raise ValueError("external_engine_sandbox.workspace_mount_path must be an absolute path")
    if not config.binary_mount_root.startswith("/"):
        raise ValueError("external_engine_sandbox.binary_mount_root must be an absolute path")
    for index, mount_path in enumerate(config.tmpfs):
        if not mount_path.startswith("/"):
            raise ValueError(f"external_engine_sandbox.tmpfs[{index}] must be an absolute path")
    normalized_policies: dict[str, ExternalEngineSandboxPolicy] = {}
    for engine_name, policy in config.engine_policies.items():
        if engine_name not in VALID_ENGINE_NAMES:
            allowed = ", ".join(sorted(VALID_ENGINE_NAMES))
            raise ValueError(
                f"external_engine_sandbox.engine_policies key must be one of: {allowed}"
            )
        normalized_policies[engine_name] = _normalize_external_engine_sandbox_policy(
            policy,
            field_name=f"external_engine_sandbox.engine_policies[{engine_name}]",
        )
    config.engine_policies = normalized_policies
    config.tmpfs = [mount_path for mount_path in config.tmpfs if mount_path.strip()]
    return config
