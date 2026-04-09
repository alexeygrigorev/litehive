"""Sandbox planning and invocation wrapping for external engine execution.

Supports two backends:
- ``docker``: container-based isolation using Docker images.
- ``bubblewrap``: lightweight namespace-based isolation using bwrap(1).
"""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil

from litehive.config import (
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    SubagentResourceLimitsConfig,
)
from litehive.agents.base import CLIInvocation
from litehive.models import ResourceLimitEvent


@dataclass(frozen=True, slots=True)
class SandboxPolicySummary:
    enabled: bool
    backend: str | None = None
    runtime: str | None = None
    image: str | None = None
    network_mode: str | None = None
    workspace_mode: str | None = None
    memory_mb: int | None = None
    cpu_count: float | None = None
    process_limit: int | None = None
    environment: tuple[str, ...] = ()
    credential_inputs: tuple[str, ...] = ()
    propagated_mounts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "runtime": self.runtime,
            "image": self.image,
            "network_mode": self.network_mode,
            "workspace_mode": self.workspace_mode,
            "memory_mb": self.memory_mb,
            "cpu_count": self.cpu_count,
            "process_limit": self.process_limit,
            "environment": list(self.environment),
            "credential_inputs": list(self.credential_inputs),
            "propagated_mounts": list(self.propagated_mounts),
        }

    @property
    def summary(self) -> str:
        if not self.enabled:
            return "host"
        if self.backend == "bubblewrap":
            details = [
                f"bwrap",
                f"net={self.network_mode}",
                f"workspace={self.workspace_mode}",
            ]
        else:
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
        if self.propagated_mounts:
            details.append(f"mounts={','.join(self.propagated_mounts)}")
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
            self.config.external_engine_sandbox.enabled and policy is not None and policy.enabled
        )
        if not sandbox_enabled:
            return SandboxPolicySummary(enabled=False)
        return SandboxPolicySummary(
            enabled=True,
            backend=self.config.external_engine_sandbox.backend,
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
            credential_inputs=tuple(
                () if policy is None else (item.env_var for item in policy.credential_inputs)
            ),
            propagated_mounts=(
                tuple(p for p in self.BWRAP_SYSTEM_RO_BINDS if Path(p).exists())
                if self.config.external_engine_sandbox.backend == "bubblewrap"
                else ()
            ),
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
            raise SandboxError(
                f"Engine '{engine_name}' is unavailable: missing binary '{binary_name}'"
            )

        if runtime_config.backend == "bubblewrap":
            return self._wrap_bubblewrap(
                engine_name,
                binary_name,
                binary_path,
                invocation,
                summary,
            )
        return self._wrap_docker(
            engine_name,
            binary_name,
            binary_path,
            invocation,
            summary,
        )

    def _wrap_docker(
        self,
        engine_name: str,
        binary_name: str,
        binary_path: str,
        invocation: CLIInvocation,
        summary: SandboxPolicySummary,
    ) -> CLIInvocation:
        runtime_config = self.config.external_engine_sandbox
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
        container_binary_path = (
            PurePosixPath(runtime_config.binary_mount_root) / mounted_binary_name
        )
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
                self._bind_mount_spec(
                    Path(binary_path).resolve(), container_binary_path, read_only=True
                ),
            ]
        )

        allowed_env: dict[str, str] = {}
        for env_name in () if policy is None else policy.environment:
            value = invocation.env.get(env_name)
            if value is not None:
                allowed_env[env_name] = value
        for credential in () if policy is None else policy.credential_inputs:
            raw_path = invocation.env.get(credential.env_var)
            if not raw_path:
                continue
            host_path = Path(raw_path).expanduser().resolve()
            argv.extend(
                [
                    "--mount",
                    self._bind_mount_spec(
                        host_path, PurePosixPath(credential.mount_path), read_only=True
                    ),
                ]
            )
            allowed_env[credential.env_var] = credential.mount_path

        for env_name, value in sorted(allowed_env.items()):
            argv.extend(["--env", f"{env_name}={value}"])

        argv.append(runtime_config.image)
        argv.extend(container_argv)
        return CLIInvocation(argv=tuple(argv), cwd=invocation.cwd, env=invocation.env)

    # Minimal read-only system paths exposed to the bubblewrap sandbox.
    BWRAP_SYSTEM_RO_BINDS: tuple[str, ...] = (
        "/usr",
        "/lib",
        "/lib64",
        "/bin",
        "/sbin",
        "/etc/alternatives",
        "/etc/resolv.conf",
        "/etc/ssl",
        "/etc/ca-certificates",
        "/etc/ld.so.cache",
    )

    def _wrap_bubblewrap(
        self,
        engine_name: str,
        binary_name: str,
        binary_path: str,
        invocation: CLIInvocation,
        summary: SandboxPolicySummary,
    ) -> CLIInvocation:
        runtime_config = self.config.external_engine_sandbox
        policy = self._policy_for_engine(engine_name)
        workspace_mode = (
            runtime_config.default_workspace_mode
            if policy is None or policy.workspace_mode is None
            else policy.workspace_mode
        )

        argv: list[str] = [runtime_config.runtime_binary]
        argv.extend(runtime_config.runtime_args)

        # Namespace isolation.
        argv.append("--unshare-all")
        if (summary.network_mode or runtime_config.default_network_mode) != "none":
            argv.append("--share-net")
        argv.append("--die-with-parent")

        # Basic virtual filesystems.
        argv.extend(["--proc", "/proc"])
        argv.extend(["--dev", "/dev"])
        for tmpfs_path in runtime_config.tmpfs:
            argv.extend(["--tmpfs", tmpfs_path])

        # Read-only system mounts (only existing paths).
        for sys_path in self.BWRAP_SYSTEM_RO_BINDS:
            if Path(sys_path).exists():
                argv.extend(["--ro-bind", sys_path, sys_path])

        # Workspace mount.
        workspace_root = str(self.root)
        if workspace_mode == "ro":
            argv.extend(["--ro-bind", workspace_root, workspace_root])
        else:
            argv.extend(["--bind", workspace_root, workspace_root])

        # Engine binary (read-only, at its host path).
        resolved_binary = str(Path(binary_path).resolve())
        if not resolved_binary.startswith(workspace_root + os.sep):
            argv.extend(["--ro-bind", resolved_binary, resolved_binary])

        # Credential mounts.
        for credential in () if policy is None else policy.credential_inputs:
            raw_path = invocation.env.get(credential.env_var)
            if not raw_path:
                continue
            host_path = str(Path(raw_path).expanduser().resolve())
            argv.extend(["--ro-bind", host_path, credential.mount_path])

        # Clear environment and propagate only allowed variables.
        argv.append("--clearenv")
        allowed_env: dict[str, str] = {}
        for env_name in () if policy is None else policy.environment:
            value = invocation.env.get(env_name)
            if value is not None:
                allowed_env[env_name] = value
        for credential in () if policy is None else policy.credential_inputs:
            if invocation.env.get(credential.env_var):
                allowed_env[credential.env_var] = credential.mount_path
        # Always propagate PATH and HOME for basic tool operation.
        for builtin_var in ("PATH", "HOME"):
            if builtin_var not in allowed_env:
                value = invocation.env.get(builtin_var, os.environ.get(builtin_var, ""))
                if value:
                    allowed_env[builtin_var] = value
        for env_name, value in sorted(allowed_env.items()):
            argv.extend(["--setenv", env_name, value])

        # Working directory and command.
        argv.extend(["--chdir", workspace_root])
        argv.append("--")
        argv.extend(invocation.argv)

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
        if any(
            marker in text
            for marker in (
                "oomkilled",
                "oom killed",
                "out of memory",
                "oom",
                "cannot allocate memory",
            )
        ):
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
            limit is not None
            for limit in (summary.memory_mb, summary.cpu_count, summary.process_limit)
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
