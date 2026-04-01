"""Docker sandbox planning and invocation wrapping for external engine execution."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil

from litehive.config import (
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    SubagentResourceLimitsConfig,
)
from litehive.external_cli import CLIInvocation
from litehive.models import ResourceLimitEvent


@dataclass(frozen=True, slots=True)
class SandboxPolicySummary:
    enabled: bool
    runtime: str | None = None
    image: str | None = None
    network_mode: str | None = None
    workspace_mode: str | None = None
    memory_mb: int | None = None
    cpu_count: float | None = None
    process_limit: int | None = None
    environment: tuple[str, ...] = ()
    credential_inputs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "runtime": self.runtime,
            "image": self.image,
            "network_mode": self.network_mode,
            "workspace_mode": self.workspace_mode,
            "memory_mb": self.memory_mb,
            "cpu_count": self.cpu_count,
            "process_limit": self.process_limit,
            "environment": list(self.environment),
            "credential_inputs": list(self.credential_inputs),
        }

    @property
    def summary(self) -> str:
        if not self.enabled:
            return "host"
        details = [
            f"{self.runtime}:{self.image}",
            f"net={self.network_mode}",
            f"workspace={self.workspace_mode}",
        ]
        limit_parts: list[str] = []
        if self.memory_mb is not None:
            limit_parts.append(f"memory={self.memory_mb}m")
        if self.cpu_count is not None:
            limit_parts.append(f"cpus={self.cpu_count:g}")
        if self.process_limit is not None:
            limit_parts.append(f"pids={self.process_limit}")
        if limit_parts:
            details.append("limits=" + ",".join(limit_parts))
        if self.environment:
            details.append(f"env={','.join(self.environment)}")
        if self.credential_inputs:
            details.append(f"creds={','.join(self.credential_inputs)}")
        return "sandbox[" + " ".join(details) + "]"


class SandboxError(RuntimeError):
    """Raised when sandbox configuration cannot be applied."""


class SandboxLauncher:
    def __init__(self, root: Path, config: LitehiveConfig) -> None:
        self.root = root.resolve()
        self.config = config

    def policy_summary(self, engine_name: str) -> SandboxPolicySummary:
        policy = self._policy_for_engine(engine_name)
        limits = self.config.subagent_resource_limits
        sandbox_enabled = bool(limits.enabled) or (
            self.config.external_engine_sandbox.enabled
            and policy is not None
            and policy.enabled
        )
        if not sandbox_enabled:
            return SandboxPolicySummary(enabled=False)
        return SandboxPolicySummary(
            enabled=True,
            runtime=self.config.external_engine_sandbox.runtime_binary,
            image=self.config.external_engine_sandbox.image,
            network_mode=(
                self.config.external_engine_sandbox.default_network_mode
                if policy is None or policy.network_mode is None
                else policy.network_mode
            ),
            workspace_mode=(
                self.config.external_engine_sandbox.default_workspace_mode
                if policy is None or policy.workspace_mode is None
                else policy.workspace_mode
            ),
            memory_mb=limits.memory_mb if limits.enabled else None,
            cpu_count=limits.cpu_count if limits.enabled else None,
            process_limit=limits.process_limit if limits.enabled else None,
            environment=tuple(() if policy is None else policy.environment),
            credential_inputs=tuple(() if policy is None else (item.env_var for item in policy.credential_inputs)),
        )

    def wrap_invocation(
        self,
        engine_name: str,
        binary_name: str,
        invocation: CLIInvocation,
    ) -> CLIInvocation:
        summary = self.policy_summary(engine_name)
        if not summary.enabled:
            return invocation

        runtime_config = self.config.external_engine_sandbox
        runtime_path = shutil.which(runtime_config.runtime_binary)
        if runtime_path is None:
            raise SandboxError(
                f"Sandbox runtime '{runtime_config.runtime_binary}' is unavailable for engine '{engine_name}'."
            )
        binary_path = shutil.which(binary_name)
        if binary_path is None:
            raise SandboxError(f"Engine '{engine_name}' is unavailable: missing binary '{binary_name}'")

        policy = self._policy_for_engine(engine_name)
        workspace_mount = PurePosixPath(runtime_config.workspace_mount_path)
        workspace_mode = (
            runtime_config.default_workspace_mode
            if policy is None or policy.workspace_mode is None
            else policy.workspace_mode
        )
        container_argv = self._translate_container_argv(
            invocation.argv,
            host_root=self.root,
            container_root=workspace_mount,
        )

        mounted_binary_name = Path(binary_path).name
        container_binary_path = PurePosixPath(runtime_config.binary_mount_root) / mounted_binary_name
        if container_argv:
            container_argv[0] = str(container_binary_path)

        argv: list[str] = [runtime_config.runtime_binary, "run", "--rm", "--init"]
        argv.extend(runtime_config.runtime_args)
        argv.extend(["--workdir", str(workspace_mount)])
        argv.extend(["--network", summary.network_mode or runtime_config.default_network_mode])
        if summary.memory_mb is not None:
            argv.extend(["--memory", f"{summary.memory_mb}m"])
        if summary.cpu_count is not None:
            argv.extend(["--cpus", f"{summary.cpu_count:g}"])
        if summary.process_limit is not None:
            argv.extend(["--pids-limit", str(summary.process_limit)])
        if runtime_config.read_only_rootfs:
            argv.append("--read-only")
        if runtime_config.drop_capabilities:
            argv.extend(["--cap-drop", "ALL"])
        if runtime_config.no_new_privileges:
            argv.extend(["--security-opt", "no-new-privileges"])
        for tmpfs_path in runtime_config.tmpfs:
            argv.extend(["--tmpfs", tmpfs_path])

        argv.extend(
            [
                "--mount",
                self._bind_mount_spec(
                    self.root,
                    workspace_mount,
                    read_only=workspace_mode == "ro",
                ),
                "--mount",
                self._bind_mount_spec(Path(binary_path).resolve(), container_binary_path, read_only=True),
            ]
        )

        allowed_env: dict[str, str] = {}
        for env_name in (() if policy is None else policy.environment):
            value = invocation.env.get(env_name)
            if value is not None:
                allowed_env[env_name] = value
        for credential in (() if policy is None else policy.credential_inputs):
            raw_path = invocation.env.get(credential.env_var)
            if not raw_path:
                continue
            host_path = Path(raw_path).expanduser().resolve()
            argv.extend(
                [
                    "--mount",
                    self._bind_mount_spec(host_path, PurePosixPath(credential.mount_path), read_only=True),
                ]
            )
            allowed_env[credential.env_var] = credential.mount_path

        for env_name, value in sorted(allowed_env.items()):
            argv.extend(["--env", f"{env_name}={value}"])

        argv.append(runtime_config.image)
        argv.extend(container_argv)
        return CLIInvocation(argv=tuple(argv), cwd=invocation.cwd, env=invocation.env)

    def _policy_for_engine(self, engine_name: str) -> ExternalEngineSandboxPolicy | None:
        return self.config.external_engine_sandbox.engine_policies.get(engine_name)

    def classify_resource_limit_event(
        self,
        engine_name: str,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> ResourceLimitEvent | None:
        summary = self.policy_summary(engine_name)
        if not summary.enabled:
            return None
        limits = self.config.subagent_resource_limits
        if not limits.enabled:
            return None

        text = "\n".join(part for part in (stdout, stderr) if part).lower()
        if any(marker in text for marker in ("oomkilled", "oom killed", "out of memory", "oom", "cannot allocate memory")):
            return self._resource_limit_event(
                limits,
                resource="memory",
                reason="memory limit exceeded (OOM)",
                observed_signal="oom",
                exit_code=exit_code,
            )
        if any(
            marker in text
            for marker in (
                "pids limit",
                "fork rejected by pids controller",
                "resource temporarily unavailable",
            )
        ):
            return self._resource_limit_event(
                limits,
                resource="processes",
                reason="process limit exceeded",
                observed_signal="pids_limit",
                exit_code=exit_code,
            )
        if any(
            marker in text
            for marker in (
                "cpu quota exceeded",
                "cpu time limit exceeded",
                "cpu cfs quota",
                "cgroup cpu limit",
                "max cpu time exceeded",
            )
        ):
            return self._resource_limit_event(
                limits,
                resource="cpu",
                reason="CPU limit exceeded",
                observed_signal="cpu_limit",
                exit_code=exit_code,
            )
        if exit_code == 137 and summary.memory_mb is not None:
            return self._resource_limit_event(
                limits,
                resource="memory",
                reason="memory limit exceeded (exit 137)",
                observed_signal="exit_137",
                exit_code=exit_code,
            )
        if "cgroup" in text and any(
            limit is not None for limit in (summary.memory_mb, summary.cpu_count, summary.process_limit)
        ):
            return self._resource_limit_event(
                limits,
                resource="resource",
                reason="resource control limit exceeded",
                observed_signal="cgroup_limit",
                exit_code=exit_code,
            )
        return None

    @staticmethod
    def _resource_limit_event(
        limits: SubagentResourceLimitsConfig,
        *,
        resource: str,
        reason: str,
        observed_signal: str,
        exit_code: int,
    ) -> ResourceLimitEvent:
        return ResourceLimitEvent(
            resource=resource,  # type: ignore[arg-type]
            reason=reason,
            observed_signal=observed_signal,
            exit_code=exit_code,
            memory_mb=limits.memory_mb,
            cpu_count=limits.cpu_count,
            process_limit=limits.process_limit,
        )

    @staticmethod
    def _translate_container_argv(
        argv: tuple[str, ...],
        *,
        host_root: Path,
        container_root: PurePosixPath,
    ) -> list[str]:
        translated: list[str] = []
        host_root_text = str(host_root)
        for arg in argv:
            if arg == host_root_text:
                translated.append(str(container_root))
                continue
            if arg.startswith(host_root_text + os.sep):
                relative = Path(arg).resolve().relative_to(host_root)
                translated.append(str(container_root / relative.as_posix()))
                continue
            translated.append(arg)
        return translated

    @staticmethod
    def _bind_mount_spec(source: Path, target: PurePosixPath, *, read_only: bool) -> str:
        mode = ",readonly" if read_only else ""
        return f"type=bind,src={source},dst={target}{mode}"
