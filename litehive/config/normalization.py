"""Normalization and validation helpers for configuration values."""

import re
from typing import Mapping, Sequence

from litehive.config.constants import (
    ENGINE_CATEGORY_RETRY_SELECTOR_PREFIX,
    MODEL_FAMILY_RETRY_SELECTOR_PREFIX,
    VALID_AGENT_STARTUP_GUIDANCE_KEYS,
    VALID_ENGINE_NAMES,
    VALID_EXECUTION_RETRY_CLASSIFICATIONS,
    VALID_EXECUTION_RETRY_SELECTORS,
    VALID_RUNNER_HOOK_POINTS,
    VALID_SANDBOX_BACKENDS,
    VALID_SANDBOX_NETWORK_MODES,
    VALID_SANDBOX_WORKSPACE_MODES,
)
from litehive.config.dataclasses import (
    ExecutionRetryPolicy,
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    RunnerHookConfig,
    SandboxCredentialInput,
    SubagentResourceLimitsConfig,
)


def _normalize_execution_retry_selector(selector: str) -> str:
    normalized = selector.strip().lower()
    if normalized in VALID_EXECUTION_RETRY_SELECTORS:
        return normalized
    if normalized == f"{ENGINE_CATEGORY_RETRY_SELECTOR_PREFIX}external_cli":
        return "external_cli"
    if normalized.startswith(MODEL_FAMILY_RETRY_SELECTOR_PREFIX):
        family = normalized.removeprefix(MODEL_FAMILY_RETRY_SELECTOR_PREFIX).strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", family):
            return f"{MODEL_FAMILY_RETRY_SELECTOR_PREFIX}{family}"
    raise ValueError(
        "execution_retry_policies key must be an engine name, `external_cli`, "
        "or `model_family:<family>`"
    )


def _normalize_engine_sequence(engines: Sequence[str], *, field_name: str) -> list[str]:
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


def _normalize_engine_int_map(
    values: Mapping[str, int] | None,
    *,
    field_name: str,
) -> dict[str, int]:
    if values is None:
        return {}

    normalized: dict[str, int] = {}
    for engine_name, raw_value in values.items():
        if engine_name not in VALID_ENGINE_NAMES:
            allowed = ", ".join(sorted(VALID_ENGINE_NAMES))
            raise ValueError(f"{field_name} engine must be one of: {allowed}")
        if not isinstance(raw_value, int):
            raise ValueError(f"{field_name}[{engine_name}] must be an integer")
        if raw_value < 0:
            raise ValueError(f"{field_name}[{engine_name}] must be 0 or greater")
        normalized[engine_name] = raw_value
    return normalized


def _normalize_agent_startup_guidance(
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


def _normalize_runner_hook_config(
    raw_hook: RunnerHookConfig | Mapping[str, object],
    *,
    field_name: str,
) -> RunnerHookConfig:
    hook = (
        raw_hook if isinstance(raw_hook, RunnerHookConfig) else RunnerHookConfig(**dict(raw_hook))
    )
    hook.command = hook.command.strip()
    if not hook.command:
        raise ValueError(f"{field_name}.command must not be empty")
    return hook


def _normalize_runner_hooks(
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
            )
            for index, hook in enumerate(hooks)
        ]
    return normalized


_PROFILE_RESOURCE_LIMIT_DEFAULTS: dict[str, SubagentResourceLimitsConfig] = {
    "rust": SubagentResourceLimitsConfig(
        enabled=True,
        memory_mb=8192,
        cpu_count=4.0,
        process_limit=512,
    ),
    "cpp": SubagentResourceLimitsConfig(
        enabled=True,
        memory_mb=12288,
        cpu_count=6.0,
        process_limit=1024,
    ),
}


def _normalize_subagent_resource_limits(
    raw_limits: SubagentResourceLimitsConfig | Mapping[str, object] | None,
    *,
    process_profile: str,
) -> SubagentResourceLimitsConfig:
    if raw_limits is None:
        limits = SubagentResourceLimitsConfig()
    elif isinstance(raw_limits, SubagentResourceLimitsConfig):
        limits = raw_limits
    else:
        raw_enabled = raw_limits.get("enabled")
        limits = SubagentResourceLimitsConfig(
            enabled=None if raw_enabled is None else bool(raw_enabled),
            memory_mb=(
                None if raw_limits.get("memory_mb") is None else int(raw_limits.get("memory_mb"))
            ),
            cpu_count=(
                None if raw_limits.get("cpu_count") is None else float(raw_limits.get("cpu_count"))
            ),
            process_limit=(
                None
                if raw_limits.get("process_limit") is None
                else int(raw_limits.get("process_limit"))
            ),
        )

    defaults = _PROFILE_RESOURCE_LIMIT_DEFAULTS.get(process_profile)
    if limits.enabled is None:
        limits.enabled = False if defaults is None else defaults.enabled
    if limits.memory_mb is None and defaults is not None:
        limits.memory_mb = defaults.memory_mb
    if limits.cpu_count is None and defaults is not None:
        limits.cpu_count = defaults.cpu_count
    if limits.process_limit is None and defaults is not None:
        limits.process_limit = defaults.process_limit

    if limits.memory_mb is not None and limits.memory_mb <= 0:
        raise ValueError("subagent_resource_limits.memory_mb must be greater than 0")
    if limits.cpu_count is not None and limits.cpu_count <= 0:
        raise ValueError("subagent_resource_limits.cpu_count must be greater than 0")
    if limits.process_limit is not None and limits.process_limit <= 0:
        raise ValueError("subagent_resource_limits.process_limit must be greater than 0")
    return limits


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
        )
    for index, env_name in enumerate(policy.environment):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise ValueError(
                f"{field_name}.environment[{index}] must be an uppercase environment variable name"
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
    return policy


def _normalize_external_engine_sandbox_config(
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
            runtime_binary=str(
                raw_config.get("runtime_binary", "bwrap" if backend == "bubblewrap" else "docker")
            ),
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
                f"external_engine_sandbox.engine_policies engine must be one of: {allowed}"
            )
        normalized_policies[engine_name] = _normalize_external_engine_sandbox_policy(
            policy,
            field_name=f"external_engine_sandbox.engine_policies[{engine_name}]",
        )
    config.engine_policies = normalized_policies
    return config


def _normalize_execution_retry_policy(
    raw_policy: ExecutionRetryPolicy | Mapping[str, object],
    *,
    field_name: str,
) -> ExecutionRetryPolicy:
    policy = (
        raw_policy
        if isinstance(raw_policy, ExecutionRetryPolicy)
        else ExecutionRetryPolicy(**dict(raw_policy))
    )
    if policy.max_retries < 0:
        raise ValueError(f"{field_name}.max_retries must be 0 or greater")
    if policy.backoff_seconds < 0:
        raise ValueError(f"{field_name}.backoff_seconds must be 0 or greater")
    if policy.backoff_multiplier < 1:
        raise ValueError(f"{field_name}.backoff_multiplier must be 1 or greater")

    normalized_retry_on: list[str] = []
    seen: set[str] = set()
    for classification in policy.retry_on:
        if classification not in VALID_EXECUTION_RETRY_CLASSIFICATIONS:
            allowed = ", ".join(sorted(VALID_EXECUTION_RETRY_CLASSIFICATIONS))
            raise ValueError(f"{field_name}.retry_on must be one of: {allowed}")
        if classification in seen:
            continue
        seen.add(classification)
        normalized_retry_on.append(classification)
    policy.retry_on = normalized_retry_on
    return policy


def _normalize_execution_retry_policies(
    raw_policies: Mapping[str, ExecutionRetryPolicy | Mapping[str, object]] | None,
) -> dict[str, ExecutionRetryPolicy]:
    if raw_policies is None:
        return {}

    normalized: dict[str, ExecutionRetryPolicy] = {}
    for raw_selector, raw_policy in raw_policies.items():
        selector = _normalize_execution_retry_selector(raw_selector)
        normalized[selector] = _normalize_execution_retry_policy(
            raw_policy,
            field_name=f"execution_retry_policies[{selector}]",
        )
    return normalized
