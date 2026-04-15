"""Sandbox planning and invocation wrapping for external engine execution.

Backed by docker: container-based isolation using a per-engine image.
"""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil

from litehive.config.model import LitehiveConfig
from litehive.config.model import ExternalEngineSandboxPolicy
from heru.base import CLIExecutionResult, CLIInvocation, ExternalCLIAdapter
from heru.engine_detection import (
    ORIGINAL_EXTERNAL_ADAPTER_RUN,
    ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE,
    effective_engine_callable,
    filter_supported_kwargs,
    has_callable_override,
)
from litehive.domain.runtime import ResourceLimitEvent


def _forced_engine_rw_state_dirs(
    engine_name: str,
    policy: "ExternalEngineSandboxPolicy | None",
) -> frozenset[Path]:
    """State dirs that an engine must be able to write into.

    Each external engine keeps session/cache state under a known directory
    (codex → ``$CODEX_HOME``, claude → ``~/.claude``, etc.). If the workspace
    sandbox policy accidentally classifies that dir as read-only, the engine
    crashes on startup with "Read-only file system" trying to record its
    session rollout. This helper returns the absolute paths that the
    SandboxLauncher must always mount read-write for the given engine, so
    we can promote them out of ``extra_ro_binds`` and into ``extra_rw_binds``
    regardless of how the workspace YAML is shaped.
    """

    setenv = {} if policy is None else dict(policy.setenv)
    home_override = setenv.get("HOME")
    home = Path(home_override).expanduser() if home_override else Path.home()

    candidates: list[Path] = []
    if engine_name == "codex":
        codex_home = setenv.get("CODEX_HOME")
        candidates.append(Path(codex_home).expanduser() if codex_home else home / ".codex")
    elif engine_name == "claude":
        candidates.append(home / ".claude")
    elif engine_name == "copilot":
        candidates.append(home / ".copilot")
    elif engine_name == "gemini":
        candidates.append(home / ".gemini")
    elif engine_name == "opencode":
        candidates.append(home / ".config" / "opencode")
    elif engine_name == "goz":
        candidates.append(home / ".goz")
        candidates.append(home / ".config" / "goz")

    resolved: set[Path] = set()
    for candidate in candidates:
        try:
            resolved.add(candidate.resolve())
        except OSError:
            continue
    return frozenset(resolved)


def _sanitize_path_env(raw_path: str) -> str:
    """Drop PATH segments that point at ephemeral codex arg0 dirs.

    Codex injects a randomly-named directory under ``$CODEX_HOME/tmp/arg0``
    into PATH when it starts, and removes it at exit. If the litehive daemon
    inherited a PATH from a shell where codex had previously run, it may
    carry a stale arg0 entry whose target no longer exists. Passing that
    through to a fresh codex process inside the sandbox makes codex emit
    "WARNING: proceeding, even though we could not update PATH: Read-only
    file system" and in older codex builds bail out entirely.
    """

    if not raw_path:
        return raw_path
    kept: list[str] = []
    for segment in raw_path.split(":"):
        if not segment:
            continue
        if "codex-arg0" in segment:
            continue
        if "codex-linux-" in segment and segment.endswith("/path"):
            continue
        kept.append(segment)
    return ":".join(kept)


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
        del role  # kept for backward-compat; docker has a single profile
        policy = self._policy_for_engine(engine_name)
        sandbox_enabled = self.config.external_engine_sandbox.enabled and policy is not None and policy.enabled
        if not sandbox_enabled:
            return SandboxPolicySummary(enabled=False)
        return SandboxPolicySummary(
            enabled=True,
            backend="docker",
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
            credential_inputs=tuple(
                () if policy is None else (item.env_var for item in policy.credential_inputs)
            ),
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
            raise SandboxError(
                f"Engine '{engine_name}' is unavailable: missing binary '{binary_name}'"
            )

        return self._wrap_docker(
            engine_name,
            role,
            binary_name,
            binary_path,
            invocation,
            summary,
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
        container_binary_path = (
            PurePosixPath(runtime_config.binary_mount_root) / mounted_binary_name
        )
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
                self._bind_mount_spec(
                    Path(binary_path).resolve(), container_binary_path, read_only=True
                ),
            ]
        )

        del role  # role no longer drives profile selection under docker
        allowed_env: dict[str, str] = {}
        for env_name in () if policy is None else policy.environment:
            value = invocation.env.get(env_name)
            if value is not None:
                allowed_env[env_name] = value
        extra_ro_binds = self._resolved_extra_ro_binds(engine_name, policy)
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

    def _policy_for_engine(self, engine_name: str) -> ExternalEngineSandboxPolicy | None:
        return self.config.external_engine_sandbox.engine_policies.get(engine_name)

    @staticmethod
    def _resolved_extra_ro_binds(
        engine_name: str,
        policy: ExternalEngineSandboxPolicy | None,
    ) -> tuple[Path, ...]:
        forced_rw = _forced_engine_rw_state_dirs(engine_name, policy)
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
    ) -> tuple[Path, ...]:
        forced_rw = _forced_engine_rw_state_dirs(engine_name, policy)
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

class SandboxedAdapter(ExternalCLIAdapter):
    def __init__(
        self, adapter: ExternalCLIAdapter, launcher: SandboxLauncher, engine_name: str, role: str
    ) -> None:
        super().__init__(
            name=adapter.name,
            binary=adapter.binary,
            capabilities=adapter.capabilities,
            stripped_env_vars=adapter.stripped_env_vars,
        )
        self._adapter = adapter
        self._launcher = launcher
        self._engine_name = engine_name
        self._role = role
        self._summary = launcher.policy_summary(engine_name, role)

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        return self._adapter.build_command(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
        )

    def detect_capabilities(self):
        return self._adapter.detect_capabilities()

    def finalize_invocation(self, invocation):
        return self._launcher.wrap_invocation(
            self._engine_name,
            self.binary,
            invocation,
            role=self._role,
        )

    def sandbox_details(self) -> tuple[bool, str]:
        return (self._summary.enabled, self._summary.summary)

    def run(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started=None,
        emit_unified: bool = False,
    ) -> CLIExecutionResult:
        if has_callable_override(self._adapter, "run", ORIGINAL_EXTERNAL_ADAPTER_RUN):
            run_callable = effective_engine_callable(self._adapter, "run")
            if not callable(run_callable):
                run_callable = self._adapter.run
            run_kwargs = {"model": model}
            if max_turns is not None:
                run_kwargs["max_turns"] = max_turns
            if resume_session_id is not None:
                run_kwargs["resume_session_id"] = resume_session_id
            if on_started is not None:
                run_kwargs["on_started"] = on_started
            run_kwargs["emit_unified"] = emit_unified
            return run_callable(
                prompt,
                cwd,
                **filter_supported_kwargs(run_callable, run_kwargs),
            )
        return super().run(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
            on_started=on_started,
            emit_unified=emit_unified,
        )

    def run_live(
        self,
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        on_started=None,
        on_update=None,
        inactivity_timeout_seconds: float = 0,
        emit_unified: bool = False,
    ) -> CLIExecutionResult:
        if has_callable_override(self._adapter, "run_live", ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE):
            run_live_callable = effective_engine_callable(self._adapter, "run_live")
            if not callable(run_live_callable):
                run_live_callable = self._adapter.run_live
            run_live_kwargs = {"model": model}
            if max_turns is not None:
                run_live_kwargs["max_turns"] = max_turns
            if resume_session_id is not None:
                run_live_kwargs["resume_session_id"] = resume_session_id
            if on_started is not None:
                run_live_kwargs["on_started"] = on_started
            if on_update is not None:
                run_live_kwargs["on_update"] = on_update
            if inactivity_timeout_seconds > 0:
                run_live_kwargs["inactivity_timeout_seconds"] = inactivity_timeout_seconds
            run_live_kwargs["emit_unified"] = emit_unified
            return run_live_callable(
                prompt,
                cwd,
                **filter_supported_kwargs(run_live_callable, run_live_kwargs),
            )
        return super().run_live(
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
            on_started=on_started,
            on_update=on_update,
            inactivity_timeout_seconds=inactivity_timeout_seconds,
            emit_unified=emit_unified,
        )

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self._adapter.render_transcript(execution)
