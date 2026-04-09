"""Supporting dataclasses for workspace configuration."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class ExecutionRetryPolicy:
    max_retries: int = 0
    backoff_seconds: float = 0.0
    backoff_multiplier: float = 1.0
    retry_on: list[str] = field(default_factory=lambda: ["timeout", "network", "service"])


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
class SubagentResourceLimitsConfig:
    enabled: bool | None = None
    memory_mb: int | None = None
    cpu_count: float | None = None
    process_limit: int | None = None


@dataclass(slots=True)
class RunnerHookConfig:
    command: str
    reject_on_failure: bool = False
    description: str | None = None
