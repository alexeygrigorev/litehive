"""Human-readable config formatting helpers."""

from litehive.config.model import LitehiveConfig


def format_external_engine_sandbox(config: LitehiveConfig) -> str:
    sandbox = config.external_engine_sandbox
    if not sandbox.enabled:
        return "disabled"
    policy_parts: list[str] = []
    for engine_name in sorted(sandbox.engine_policies):
        policy = sandbox.engine_policies[engine_name]
        envs = ",".join(policy.environment) or "-"
        creds = ",".join(item.env_var for item in policy.credential_inputs) or "-"
        binds = ",".join(policy.extra_ro_binds) or "-"
        policy_parts.append(
            f"{engine_name}=enabled:{policy.enabled} net:{policy.network_mode or sandbox.default_network_mode} "
            f"workspace:{policy.workspace_mode or sandbox.default_workspace_mode} env:{envs} creds:{creds} binds:{binds}"
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
        return f"mode:{config.runner_hook_execution_mode}; none"
    parts: list[str] = []
    for point in sorted(config.runner_hooks):
        hooks = ", ".join(
            (
                f"{'reject' if hook.reject_on_failure else 'run'}:{hook.command}"
                + (f" ({hook.description})" if hook.description else "")
            )
            for hook in config.runner_hooks[point]
        )
        parts.append(f"{point}=[{hooks}]")
    return "; ".join([f"mode:{config.runner_hook_execution_mode}", *parts])
