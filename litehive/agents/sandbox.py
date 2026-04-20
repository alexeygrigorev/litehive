"""Sandbox planning and invocation wrapping for external engine execution.

Backed by docker: container-based isolation using a per-engine image.
"""

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
from typing import Mapping

from litehive.agents._sandbox import (
    SandboxedAdapter as LitehiveSandboxedAdapter,
    forced_engine_rw_state_dirs as _forced_engine_rw_state_dirs,
    sanitize_path_env as _sanitize_path_env,
)
from heru.base import CLIInvocation
from litehive.config.model import LitehiveConfig
from litehive.config.model import ExternalEngineSandboxPolicy
from litehive.domain.runtime import ResourceLimitEvent


class SandboxProfile(str, Enum):
    NO_GIT = "no_git"
    MERGE_RESOLVER = "merge_resolver"


SandboxedAdapter = LitehiveSandboxedAdapter


def sandbox_profile_for_role(role: str) -> SandboxProfile:
    if role == "merge-resolver":
        return SandboxProfile.MERGE_RESOLVER
    return SandboxProfile.NO_GIT


@dataclass(frozen=True, slots=True)
class SandboxPolicySummary:
    enabled: bool
    backend: str | None = None
    runtime: str | None = None
    image: str | None = None
    network_mode: str | None = None
    workspace_mode: str | None = None
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
            "environment": list(self.environment),
            "credential_inputs": list(self.credential_inputs),
            "propagated_mounts": list(self.propagated_mounts),
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

    def policy_summary(self, engine_name: str, role: str = "") -> SandboxPolicySummary:
        policy = self._policy_for_engine(engine_name)
        sandbox_enabled = self.config.external_engine_sandbox.enabled and policy is not None and policy.enabled
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
            environment=tuple(() if policy is None else policy.environment),
            credential_inputs=tuple(() if policy is None else (item.env_var for item in policy.credential_inputs)),
            propagated_mounts=(),
        )

    def wrap_invocation(
        self,
        engine_name: str,
        binary_name: str,
        invocation: CLIInvocation,
        role: str = "",
    ) -> CLIInvocation:
        summary = self.policy_summary(engine_name, role)
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

        return (
            self._wrap_docker(
                engine_name,
                role,
                binary_name,
                binary_path,
                invocation,
                summary,
            )
            if runtime_config.backend == "docker"
            else self._wrap_bubblewrap(
                engine_name,
                role,
                binary_name,
                binary_path,
                invocation,
                summary,
            )
        )

    def _wrap_docker(
        self,
        engine_name: str,
        role: str,
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
        container_binary_path = PurePosixPath(runtime_config.binary_mount_root) / mounted_binary_name
        if container_argv:
            container_argv[0] = str(container_binary_path)

        argv: list[str] = [runtime_config.runtime_binary, "run", "--rm", "--init"]
        argv.extend(runtime_config.runtime_args)
        argv.extend(["--workdir", str(workspace_mount)])
        argv.extend(["--network", summary.network_mode or runtime_config.default_network_mode])
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

        del role  # role no longer drives profile selection under docker
        allowed_env: dict[str, str] = {}
        for env_name in () if policy is None else policy.environment:
            value = invocation.env.get(env_name)
            if value is not None:
                allowed_env[env_name] = value
        if policy is not None:
            allowed_env.update(policy.setenv)
        extra_ro_binds = self._resolved_extra_ro_binds(engine_name, policy, invocation.env)
        for host_path in extra_ro_binds:
            argv.extend(
                [
                    "--mount",
                    self._bind_mount_spec(host_path, PurePosixPath(str(host_path)), read_only=True),
                ]
            )
        for credential in () if policy is None else policy.credential_inputs:
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

    def _wrap_bubblewrap(
        self,
        engine_name: str,
        role: str,
        binary_name: str,
        binary_path: str,
        invocation: CLIInvocation,
        summary: SandboxPolicySummary,
    ) -> CLIInvocation:
        runtime_config = self.config.external_engine_sandbox
        policy = self._policy_for_engine(engine_name)
        profile = sandbox_profile_for_role(role)
        env = dict(invocation.env)
        if policy is not None:
            env.update(policy.setenv)

        argv: list[str] = [
            runtime_config.runtime_binary,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]
        if summary.network_mode in {"host", "bridge"}:
            argv.append("--share-net")
        argv.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])

        for system_path in ("/usr", "/bin", "/lib", "/lib64", "/sbin", "/etc"):
            host_path = Path(system_path)
            if not host_path.exists():
                continue
            self._ensure_bubblewrap_target(argv, PurePosixPath(system_path), is_dir=True)
            argv.extend(["--ro-bind", system_path, system_path])
        for system_file in ("/etc/resolv.conf",):
            host_file = Path(system_file)
            try:
                resolved_file = host_file.resolve(strict=True)
            except OSError:
                continue
            if resolved_file == host_file:
                continue
            target = PurePosixPath(str(resolved_file))
            self._ensure_bubblewrap_target(argv, target, is_dir=False)
            argv.extend(["--ro-bind", str(resolved_file), str(target)])

        self._ensure_bubblewrap_target(argv, PurePosixPath(str(self.root)), is_dir=True)
        argv.extend(["--bind", str(self.root), str(self.root)])

        for host_path in self._resolved_extra_ro_binds(engine_name, policy, env):
            target = PurePosixPath(str(host_path))
            self._ensure_bubblewrap_target(argv, target, is_dir=host_path.is_dir())
            argv.extend(["--ro-bind", str(host_path), str(target)])
        for host_path in self._resolved_extra_rw_binds(engine_name, policy, env):
            target = PurePosixPath(str(host_path))
            self._ensure_bubblewrap_target(argv, target, is_dir=host_path.is_dir())
            argv.extend(["--bind", str(host_path), str(target)])

        wrapper_paths = self._ensure_bubblewrap_wrappers()
        real_git_path = shutil.which("git")
        if profile is SandboxProfile.NO_GIT:
            env["PATH"] = f"{wrapper_paths['no_git_bin']}{os.pathsep}{env.get('PATH', '')}"
            self._ensure_bubblewrap_target(argv, PurePosixPath("/usr/bin/git"), is_dir=False)
            argv.extend(["--bind", str(wrapper_paths["no_git"]), "/usr/bin/git"])
        elif profile is SandboxProfile.MERGE_RESOLVER and real_git_path is not None:
            env["PATH"] = f"{wrapper_paths['merge_bin']}{os.pathsep}{env.get('PATH', '')}"
            env["LITEHIVE_REAL_GIT_PATH"] = "/litehive/bin/git.real"
            env["LITEHIVE_WORKSPACE_ROOT"] = str(self.root)
            env["LITEHIVE_SOURCE_ROOT"] = str(Path(__file__).resolve().parents[2])
            env["LITEHIVE_PYTHON_PATH"] = sys.executable
            self._ensure_bubblewrap_target(argv, PurePosixPath("/litehive/bin/git.real"), is_dir=False)
            argv.extend(["--ro-bind", real_git_path, "/litehive/bin/git.real"])
            source_root = Path(env["LITEHIVE_SOURCE_ROOT"]).resolve()
            self._ensure_bubblewrap_target(argv, PurePosixPath(str(source_root)), is_dir=True)
            argv.extend(["--ro-bind", str(source_root), str(source_root)])

        sanitized_path = _sanitize_path_env(env.get("PATH", ""))
        if sanitized_path:
            env["PATH"] = sanitized_path

        argv.append("--")
        argv.extend(invocation.argv)
        return CLIInvocation(argv=tuple(argv), cwd=invocation.cwd, env=env)

    @staticmethod
    def _ensure_bubblewrap_target(
        argv: list[str],
        target: PurePosixPath,
        *,
        is_dir: bool,
    ) -> None:
        candidates = list(target.parents)[::-1]
        if is_dir:
            candidates.append(target)
        else:
            candidates.append(target.parent)
        seen: set[str] = set()
        for candidate in candidates:
            text = str(candidate)
            if text in {"", "/"} or text in seen:
                continue
            seen.add(text)
            argv.extend(["--dir", text])

    def _ensure_bubblewrap_wrappers(self) -> dict[str, Path]:
        runtime_dir = self.root / ".litehive" / "runtime" / "sandbox"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        merge_bin = runtime_dir / "merge-bin"
        no_git_bin = runtime_dir / "no-git-bin"
        merge_bin.mkdir(parents=True, exist_ok=True)
        no_git_bin.mkdir(parents=True, exist_ok=True)
        merge_git = merge_bin / "git"
        no_git = no_git_bin / "git"
        merge_git.write_text(
            """#!/bin/sh
export PYTHONPATH="${LITEHIVE_SOURCE_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec "${LITEHIVE_PYTHON_PATH}" -c 'from litehive.sandbox.git_wrapper import main; import os, sys; raise SystemExit(main(sys.argv[1:], real_git_path=os.environ["LITEHIVE_REAL_GIT_PATH"], workspace_root=os.environ["LITEHIVE_WORKSPACE_ROOT"]))' "$@"
""",
            encoding="utf-8",
        )
        no_git.write_text(
            """#!/bin/sh
echo "git: not found" >&2
exit 127
""",
            encoding="utf-8",
        )
        for path in (merge_git, no_git):
            path.chmod(0o755)
        return {
            "merge_git": merge_git,
            "merge_bin": merge_bin,
            "no_git": no_git,
            "no_git_bin": no_git_bin,
        }

    def _policy_for_engine(self, engine_name: str) -> ExternalEngineSandboxPolicy | None:
        return self.config.external_engine_sandbox.engine_policies.get(engine_name)

    @staticmethod
    def _resolved_extra_ro_binds(
        engine_name: str,
        policy: ExternalEngineSandboxPolicy | None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[Path, ...]:
        forced_rw = _forced_engine_rw_state_dirs(engine_name, policy, env)
        resolved_paths: list[Path] = []
        for raw_path in () if policy is None else policy.extra_ro_binds:
            host_path = Path(raw_path).expanduser()
            if not host_path.exists():
                raise SandboxError(
                    f"Sandbox policy for engine '{engine_name}' requires read-only bind path "
                    f"'{host_path}', but it does not exist on the host."
                )
            resolved = host_path.resolve()
            if resolved in forced_rw:
                # Engine state dirs need write access (codex sessions, etc.) —
                # if the workspace policy lists them under extra_ro_binds we
                # promote them to extra_rw_binds in _resolved_extra_rw_binds
                # rather than mounting them read-only here.
                continue
            resolved_paths.append(resolved)
        return tuple(resolved_paths)

    @staticmethod
    def _resolved_extra_rw_binds(
        engine_name: str,
        policy: ExternalEngineSandboxPolicy | None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[Path, ...]:
        forced_rw = _forced_engine_rw_state_dirs(engine_name, policy, env)
        resolved_paths: list[Path] = []
        seen: set[Path] = set()
        for raw_path in () if policy is None else policy.extra_rw_binds:
            host_path = Path(raw_path).expanduser()
            if not host_path.exists():
                raise SandboxError(
                    f"Sandbox policy for engine '{engine_name}' requires read-write bind path "
                    f"'{host_path}', but it does not exist on the host."
                )
            resolved = host_path.resolve()
            if resolved not in seen:
                resolved_paths.append(resolved)
                seen.add(resolved)
        # Promote any forced-rw engine state dirs that the workspace policy
        # accidentally classified as ro (or omitted entirely) — e.g. codex
        # needs write access to $CODEX_HOME/sessions to record rollouts.
        for path in forced_rw:
            if path.exists() and path not in seen:
                resolved_paths.append(path)
                seen.add(path)
        return tuple(resolved_paths)

    def classify_resource_limit_event(
        self,
        engine_name: str,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> ResourceLimitEvent | None:
        return None

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
