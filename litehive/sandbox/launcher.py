"""Sandbox planning and invocation wrapping for external engine execution.

Backed by docker: container-based isolation using a per-engine image.
"""

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Mapping

from heru.base import CLIInvocation
from litehive.config.model import LitehiveConfig
from litehive.config.model import ExternalEngineSandboxPolicy
from litehive.sandbox.support import forced_engine_rw_state_dirs


class SandboxProfile(str, Enum):
    """Git wrapper profile applied to a sandboxed engine invocation."""

    NO_GIT = "no_git"
    MERGE_RESOLVER = "merge_resolver"

    @classmethod
    def for_role(cls, role: str) -> "SandboxProfile":
        """
        Pick the git-wrapper profile that matches a subagent role.

        The merge-resolver role is the only one allowed to touch git
        inside the sandbox through the guarded wrapper; every other
        role gets git fully blocked.
        """
        if role == "merge-resolver":
            return cls.MERGE_RESOLVER
        return cls.NO_GIT


@dataclass(frozen=True, slots=True)
class SandboxPolicySummary:
    """Snapshot of the resolved sandbox policy for one engine/role pair, surfaced to operators in subagent reports."""

    enabled: bool
    backend: str | None = None
    runtime: str | None = None
    image: str | None = None
    network_mode: str | None = None
    workspace_mode: str | None = None
    environment: tuple[str, ...] = ()
    credential_inputs: tuple[str, ...] = ()
    propagated_mounts: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "SandboxPolicySummary":
        """
        Rehydrate a policy summary from the JSON resource-control field.

        Session recovery reads historical SQLite payloads as mappings;
        this is the boundary that narrows that JSON shape back into the
        typed policy object used by report/session construction.
        """
        return cls(
            enabled=bool(payload.get("enabled")),
            backend=_optional_str(payload.get("backend")),
            runtime=_optional_str(payload.get("runtime")),
            image=_optional_str(payload.get("image")),
            network_mode=_optional_str(payload.get("network_mode")),
            workspace_mode=_optional_str(payload.get("workspace_mode")),
            environment=_string_tuple(payload.get("environment")),
            credential_inputs=_string_tuple(payload.get("credential_inputs")),
            propagated_mounts=_string_tuple(payload.get("propagated_mounts")),
        )

    def as_dict(self) -> dict[str, object]:
        """
        Serialize the policy summary for the report ``resource_control`` JSON field.

        This is intentionally limited to the persistence/reporting
        boundary; runtime code should pass the dataclass itself.
        """
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
        """
        Render the policy as a one-line ``host`` or ``sandbox[...]``
        string.

        Used in CLI output and stored on
        ``Subagent.sandbox_summary`` so the same human-readable
        form appears in every operator-facing surface; the structured
        fields are still available via :meth:`as_dict` for callers
        that need them.
        """
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


def _optional_str(value: object) -> str | None:
    """
    Return a string only when a JSON payload already contains one.
    """
    if isinstance(value, str):
        return value
    return None


def _string_tuple(value: object) -> tuple[str, ...]:
    """
    Narrow a JSON list field into the tuple shape used in policy summaries.
    """
    if not isinstance(value, list):
        return ()
    strings: list[str] = []
    for item in value:
        if isinstance(item, str):
            strings.append(item)
    return tuple(strings)


class SandboxError(RuntimeError):
    """Raised when sandbox configuration cannot be applied."""


class SandboxLauncher:
    """Builds docker-run argv that wraps every external engine invocation."""

    def __init__(self, root: Path, config: LitehiveConfig) -> None:
        """Store the resolved workspace root and the parsed config so per-engine policies can be looked up on each invocation without re-loading config."""
        self.root = root.resolve()
        self.config = config

    def policy_summary(self, engine_name: str) -> SandboxPolicySummary:
        """
        Resolve the effective sandbox policy for an engine.

        Used both by the SubagentManager (for ``resource_control``
        reporting) and internally by ``wrap_invocation``, so
        workspace-level config and per-engine overrides only need to
        be merged in one place — the two consumers always see the
        same policy shape.
        """
        policy = self._policy_for_engine(engine_name)
        sandbox_enabled = self.config.external_engine_sandbox.enabled and policy is not None and policy.enabled
        if not sandbox_enabled:
            return SandboxPolicySummary(enabled=False)
        if policy is None or policy.network_mode is None:
            network_mode = self.config.external_engine_sandbox.default_network_mode
        else:
            network_mode = policy.network_mode
        if policy is None or policy.workspace_mode is None:
            workspace_mode = self.config.external_engine_sandbox.default_workspace_mode
        else:
            workspace_mode = policy.workspace_mode
        if policy is None:
            environment_tuple: tuple = ()
            credential_inputs_tuple: tuple = ()
        else:
            environment_tuple = tuple(policy.environment)
            credential_inputs_tuple = tuple(item.env_var for item in policy.credential_inputs)
        return SandboxPolicySummary(
            enabled=True,
            backend=self.config.external_engine_sandbox.backend,
            runtime=self.config.external_engine_sandbox.runtime_binary,
            image=self.config.external_engine_sandbox.image,
            network_mode=network_mode,
            workspace_mode=workspace_mode,
            environment=environment_tuple,
            credential_inputs=credential_inputs_tuple,
            propagated_mounts=(),
        )

    def wrap_invocation(
        self,
        engine_name: str,
        binary_name: str,
        invocation: CLIInvocation,
        role: str = "",
    ) -> CLIInvocation:
        """
        Rewrite an engine CLI invocation so it runs inside the
        configured sandbox.

        Returns the original invocation unchanged when sandboxing is
        disabled for the engine, so callers (the ``SandboxedAdapter``
        wrapper around heru's ``ExternalCLIAdapter``) don't need to
        branch on policy themselves and the disabled-sandbox path
        stays a single line.
        """
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

        if runtime_config.backend != "docker":
            raise SandboxError(f"Unsupported sandbox backend '{runtime_config.backend}' for engine '{engine_name}'")

        return self._wrap_docker(
            engine_name,
            role,
            binary_path,
            invocation,
            summary,
        )

    def _wrap_docker(
        self,
        engine_name: str,
        role: str,
        binary_path: str,
        invocation: CLIInvocation,
        summary: SandboxPolicySummary,
    ) -> CLIInvocation:
        """
        Assemble the docker-run argv: mounts, env allowlist, git
        wrapper, and translated workspace paths.

        All policy decisions (network mode, workspace ro/rw,
        credential propagation, git profile) collapse into one argv
        build here so the SubagentManager only sees a single
        transformed CLIInvocation — without one assembler, each
        decision would scatter across the call sites.
        """
        runtime_config = self.config.external_engine_sandbox
        policy = self._policy_for_engine(engine_name)
        workspace_mount = PurePosixPath(runtime_config.workspace_mount_path)
        if policy is None or policy.workspace_mode is None:
            workspace_mode = runtime_config.default_workspace_mode
        else:
            workspace_mode = policy.workspace_mode
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

        # Set up git wrapper for role-based git protection
        profile = SandboxProfile.for_role(role)
        allowed_env: dict[str, str] = {}
        if policy is None:
            env_names: list = []
        else:
            env_names = list(policy.environment)
        for env_name in env_names:
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
        extra_rw_binds = self._resolved_extra_rw_binds(engine_name, policy, invocation.env)
        for host_path in extra_rw_binds:
            argv.extend(
                [
                    "--mount",
                    self._bind_mount_spec(host_path, PurePosixPath(str(host_path)), read_only=False),
                ]
            )
        if policy is None:
            credential_inputs: list = []
        else:
            credential_inputs = list(policy.credential_inputs)
        for credential in credential_inputs:
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

        # Set up git wrapper for git operation protection
        wrapper_paths = self.ensure_docker_git_wrappers()
        real_git_path = shutil.which("git")

        if profile == SandboxProfile.NO_GIT:
            # Block all git commands
            argv.extend(
                [
                    "--mount",
                    self._bind_mount_spec(wrapper_paths["no_git"], PurePosixPath("/usr/local/bin/git"), read_only=True),
                ]
            )
            allowed_env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
        elif profile == SandboxProfile.MERGE_RESOLVER and real_git_path is not None:
            # Allow git commands but with protection wrapper
            argv.extend(
                [
                    "--mount",
                    self._bind_mount_spec(
                        wrapper_paths["merge_git"], PurePosixPath("/usr/local/bin/git"), read_only=True
                    ),
                    "--mount",
                    self._bind_mount_spec(
                        Path(real_git_path).resolve(), PurePosixPath("/litehive/bin/git.real"), read_only=True
                    ),
                ]
            )
            # Mount litehive source code for the wrapper
            source_root = Path(__file__).resolve().parents[2]
            argv.extend(
                [
                    "--mount",
                    self._bind_mount_spec(source_root, PurePosixPath(str(source_root)), read_only=True),
                ]
            )
            allowed_env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
            allowed_env["LITEHIVE_REAL_GIT_PATH"] = "/litehive/bin/git.real"
            allowed_env["LITEHIVE_WORKSPACE_ROOT"] = str(workspace_mount)
            allowed_env["PYTHONPATH"] = str(source_root)

        for env_name, value in sorted(allowed_env.items()):
            argv.extend(["--env", f"{env_name}={value}"])

        argv.append(runtime_config.image)
        argv.extend(container_argv)
        return CLIInvocation(argv=tuple(argv), cwd=invocation.cwd, env=invocation.env)

    def ensure_docker_git_wrappers(self) -> dict[str, Path]:
        """
        Return checked-in git wrapper commands for the Docker sandbox.

        The wrappers (``no_git.sh`` and ``git_wrapper.py``) live in
        the source tree so each role can be mounted with the right
        wrapper without dynamically generating one — the no-git
        profile blocks every git call, the merge-resolver profile
        proxies through a guarded wrapper.
        """
        sandbox_dir = Path(__file__).resolve().parents[1] / "sandbox"
        merge_git = sandbox_dir / "git_wrapper.py"
        no_git = sandbox_dir / "no_git.sh"
        merge_git.chmod(0o755)
        no_git.chmod(0o755)

        return {
            "merge_git": merge_git,
            "no_git": no_git,
        }

    def _policy_for_engine(self, engine_name: str) -> ExternalEngineSandboxPolicy | None:
        """Return the per-engine sandbox policy from workspace config, or ``None`` when the engine has no override and the workspace defaults apply."""
        return self.config.external_engine_sandbox.engine_policies.get(engine_name)

    @staticmethod
    def _resolved_extra_ro_binds(
        engine_name: str,
        policy: ExternalEngineSandboxPolicy | None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[Path, ...]:
        """
        Filter the policy's read-only bind list.

        Drops engine state dirs that need rw access; workspace
        policies sometimes list engine state dirs (e.g.
        ``$CODEX_HOME``) under ``extra_ro_binds`` even though the
        engine must write rollouts there. The matching rw helper
        re-promotes those paths so a misconfigured policy does not
        silently break engine state writes.
        """
        forced_rw = forced_engine_rw_state_dirs(engine_name, policy, env)
        resolved_paths: list[Path] = []
        if policy is None:
            ro_binds: list = []
        else:
            ro_binds = list(policy.extra_ro_binds)
        for raw_path in ro_binds:
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
        """
        Resolve read-write bind paths and force-add missed engine state
        dirs.

        Engines like codex must write to ``$CODEX_HOME/sessions``; if
        a workspace policy classifies that path read-only or omits it
        entirely, this helper still mounts it rw so rollouts succeed
        — operators should not have to know which engine paths are
        write-required.
        """
        forced_rw = forced_engine_rw_state_dirs(engine_name, policy, env)
        resolved_paths: list[Path] = []
        seen: set[Path] = set()
        if policy is None:
            rw_binds: list = []
        else:
            rw_binds = list(policy.extra_rw_binds)
        for raw_path in rw_binds:
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

    @staticmethod
    def _translate_container_argv(
        argv: tuple[str, ...],
        host_root: Path,
        container_root: PurePosixPath,
    ) -> list[str]:
        """
        Rewrite host workspace paths in argv to their container paths.

        The engine builds argv with absolute host paths (workspace
        root, prompt files, etc.); inside the container those paths
        resolve to the bind-mount root, so we substitute the prefix
        before exec — without this, the engine would dereference
        host paths that don't exist in the container.
        """
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
    def _bind_mount_spec(source: Path, target: PurePosixPath, read_only: bool) -> str:
        """Format a single ``--mount type=bind,...`` argument for ``docker run`` — centralized so the read-only flag and key order stay consistent across every mount we add to the argv."""
        if read_only:
            mode = ",readonly"
        else:
            mode = ""
        return f"type=bind,src={source},dst={target}{mode}"
