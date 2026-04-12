"""Sandbox planning and invocation wrapping for external engine execution.

Supports two backends:
- ``docker``: container-based isolation using Docker images.
- ``bubblewrap``: lightweight namespace-based isolation using bwrap(1).
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat

from litehive.config import (
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
)
from litehive.agents.base import CLIExecutionResult, CLIInvocation, ExternalCLIAdapter
from litehive.agents.engine_detection import (
    ORIGINAL_EXTERNAL_ADAPTER_RUN,
    ORIGINAL_EXTERNAL_ADAPTER_RUN_LIVE,
    effective_engine_callable,
    filter_supported_kwargs,
    has_callable_override,
)
from litehive.models import ResourceLimitEvent


@dataclass(frozen=True, slots=True)
class SandboxPolicySummary:
    enabled: bool
    profile: str = "no-git"
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
            "profile": self.profile,
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
        if self.backend == "bubblewrap":
            details = [
                "bwrap",
                f"profile={self.profile}",
                f"net={self.network_mode}",
                f"workspace={self.workspace_mode}",
            ]
        else:
            details = [
                f"{self.runtime}:{self.image}",
                f"profile={self.profile}",
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


class SandboxProfile(str, Enum):
    NO_GIT = "no-git"
    MERGE_RESOLVER = "merge-resolver"


def sandbox_profile_for_role(role: str) -> SandboxProfile:
    normalized = role.strip().lower()
    if normalized == "merge-resolver":
        return SandboxProfile.MERGE_RESOLVER
    return SandboxProfile.NO_GIT


@dataclass(frozen=True, slots=True)
class _GitFilesystemPlan:
    profile: SandboxProfile
    prepend_path: tuple[str, ...] = ()
    extra_ro_binds: tuple[tuple[str, str], ...] = ()


class SandboxLauncher:
    def __init__(self, root: Path, config: LitehiveConfig) -> None:
        self.root = root.resolve()
        self.config = config

    def policy_summary(self, engine_name: str, role: str = "") -> SandboxPolicySummary:
        policy = self._policy_for_engine(engine_name)
        profile = sandbox_profile_for_role(role)
        sandbox_enabled = self.config.external_engine_sandbox.enabled and policy is not None and policy.enabled
        if not sandbox_enabled:
            return SandboxPolicySummary(enabled=False, profile=profile.value)
        return SandboxPolicySummary(
            enabled=True,
            profile=profile.value,
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

        if runtime_config.backend == "bubblewrap":
            return self._wrap_bubblewrap(
                engine_name,
                role,
                binary_name,
                binary_path,
                invocation,
                summary,
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

        allowed_env: dict[str, str] = {}
        for env_name in () if policy is None else policy.environment:
            value = invocation.env.get(env_name)
            if value is not None:
                allowed_env[env_name] = value
        git_plan = self._prepare_git_filesystem(role)
        extra_ro_binds = self._resolved_extra_ro_binds(engine_name, policy)
        if git_plan.prepend_path:
            existing_path = invocation.env.get("PATH", os.environ.get("PATH", ""))
            segments = [*git_plan.prepend_path]
            if existing_path:
                segments.append(existing_path)
            allowed_env["PATH"] = ":".join(segments)
        for source, target in git_plan.extra_ro_binds:
            argv.extend(["--mount", self._bind_mount_spec(Path(source), PurePosixPath(target), read_only=True)])
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
        role: str,
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

        # Namespace isolation. Avoid hard-failing on hosts that forbid unprivileged
        # user namespaces while still isolating the filesystem and process tree.
        argv.extend(["--unshare-ipc", "--unshare-pid", "--unshare-uts", "--unshare-cgroup-try"])
        if (summary.network_mode or runtime_config.default_network_mode) == "none":
            argv.append("--unshare-net")
        argv.append("--die-with-parent")

        # Basic virtual filesystems.
        argv.extend(["--proc", "/proc"])
        argv.extend(["--dev", "/dev"])
        for tmpfs_path in runtime_config.tmpfs:
            argv.extend(["--tmpfs", tmpfs_path])

        git_plan = self._prepare_git_filesystem(role)
        extra_ro_binds = self._resolved_extra_ro_binds(engine_name, policy)

        # Read-only system mounts (only existing paths).
        for sys_path in self.BWRAP_SYSTEM_RO_BINDS:
            if Path(sys_path).exists():
                argv.extend(["--ro-bind", sys_path, sys_path])

        for source, target in git_plan.extra_ro_binds:
            argv.extend(["--ro-bind", source, target])
        for host_path in extra_ro_binds:
            host_path_str = str(host_path)
            argv.extend(["--ro-bind", host_path_str, host_path_str])

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
        if git_plan.prepend_path:
            existing_path = allowed_env.get("PATH", "")
            segments = [*git_plan.prepend_path]
            if existing_path:
                segments.append(existing_path)
            allowed_env["PATH"] = ":".join(segments)
        for env_name, value in sorted(allowed_env.items()):
            argv.extend(["--setenv", env_name, value])

        # Working directory and command.
        sandbox_argv = list(invocation.argv)
        if sandbox_argv:
            sandbox_argv[0] = resolved_binary
        argv.extend(["--chdir", workspace_root])
        argv.append("--")
        argv.extend(sandbox_argv)

        return CLIInvocation(argv=tuple(argv), cwd=invocation.cwd, env=invocation.env)

    def _policy_for_engine(self, engine_name: str) -> ExternalEngineSandboxPolicy | None:
        return self.config.external_engine_sandbox.engine_policies.get(engine_name)

    @staticmethod
    def _resolved_extra_ro_binds(
        engine_name: str,
        policy: ExternalEngineSandboxPolicy | None,
    ) -> tuple[Path, ...]:
        resolved_paths: list[Path] = []
        for raw_path in () if policy is None else policy.extra_ro_binds:
            host_path = Path(raw_path).expanduser()
            if not host_path.exists():
                raise SandboxError(
                    f"Sandbox policy for engine '{engine_name}' requires read-only bind path "
                    f"'{host_path}', but it does not exist on the host."
                )
            resolved_paths.append(host_path.resolve())
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

    def _prepare_git_filesystem(self, role: str) -> _GitFilesystemPlan:
        profile = sandbox_profile_for_role(role)
        runtime_root = self.root / ".litehive" / "runtime" / "sandbox"
        runtime_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:12]
        profile_root = runtime_root / f"{profile.value}-{digest}"
        profile_root.mkdir(parents=True, exist_ok=True)

        binds: list[tuple[str, str]] = []
        for host_dir, sandbox_dir in (
            (Path("/usr/bin"), "/usr/bin"),
            (Path("/bin"), "/bin"),
            (Path("/usr/sbin"), "/usr/sbin"),
            (Path("/sbin"), "/sbin"),
            (Path("/usr/lib/git-core"), "/usr/lib/git-core"),
        ):
            if not host_dir.exists():
                continue
            mirror = profile_root / sandbox_dir.lstrip("/").replace("/", "-")
            self._sync_git_filtered_mirror(host_dir, mirror)
            binds.append((str(mirror), sandbox_dir))

        prepend_path: list[str] = []
        if profile is SandboxProfile.MERGE_RESOLVER:
            wrapper_dir = profile_root / "sandbox-bin"
            hidden_dir = profile_root / "sandbox-internal"
            wrapper_dir.mkdir(parents=True, exist_ok=True)
            hidden_dir.mkdir(parents=True, exist_ok=True)
            real_git_host = shutil.which("git")
            if real_git_host is None:
                raise SandboxError("git is unavailable on the host; merge-resolver profile cannot be prepared.")
            hidden_git = hidden_dir / "git"
            if not hidden_git.exists():
                shutil.copy2(real_git_host, hidden_git)
                hidden_git.chmod(
                    stat.S_IRUSR
                    | stat.S_IXUSR
                    | stat.S_IRGRP
                    | stat.S_IXGRP
                    | stat.S_IROTH
                    | stat.S_IXOTH
                )
            wrapper_path = wrapper_dir / "git"
            wrapper_path.write_text(
                self._render_git_wrapper_script(
                    real_git_path="/sandbox/internal/git",
                    workspace_root=str(self.root),
                ),
                encoding="utf-8",
            )
            wrapper_path.chmod(
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
            binds.append((str(wrapper_dir), "/sandbox/bin"))
            binds.append((str(hidden_dir), "/sandbox/internal"))
            prepend_path.append("/sandbox/bin")
        return _GitFilesystemPlan(
            profile=profile,
            prepend_path=tuple(prepend_path),
            extra_ro_binds=tuple(binds),
        )

    @staticmethod
    def _sync_git_filtered_mirror(source_dir: Path, mirror_dir: Path) -> None:
        mirror_dir.mkdir(parents=True, exist_ok=True)
        expected: set[str] = set()
        for entry in source_dir.iterdir():
            if entry.name == "git":
                continue
            target = mirror_dir / entry.name
            expected.add(entry.name)
            if entry.is_symlink():
                link_target = os.readlink(entry)
                if target.is_symlink() and os.readlink(target) == link_target:
                    continue
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                target.symlink_to(link_target)
                continue
            if entry.is_file():
                try:
                    if target.exists() and not target.is_symlink():
                        source_stat = entry.stat()
                        target_stat = target.stat()
                        if (
                            source_stat.st_dev == target_stat.st_dev
                            and source_stat.st_ino == target_stat.st_ino
                        ):
                            continue
                except OSError:
                    pass
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                try:
                    os.link(entry, target)
                except OSError:
                    shutil.copy2(entry, target)
                continue
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.copytree(entry, target, symlinks=True)
        for existing in mirror_dir.iterdir():
            if existing.name not in expected:
                if existing.is_dir() and not existing.is_symlink():
                    shutil.rmtree(existing)
                else:
                    existing.unlink()

    @staticmethod
    def _render_git_wrapper_script(*, real_git_path: str, workspace_root: str) -> str:
        return f"""#!/usr/bin/env python3
from datetime import UTC, datetime
import os
from pathlib import Path
import sys

PROTECTED_REFS = {{"main", "master", "origin/main", "origin/master"}}

def non_option_args(argv):
    return [arg for arg in argv if arg and not arg.startswith("-")]

def is_origin_ref(value):
    return value.startswith("origin/")

def is_protected_ref(value):
    return value in PROTECTED_REFS or value.startswith("origin/") or value.startswith("refs/remotes/")

def resolve_git_dir(cwd):
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        git_entry = candidate / ".git"
        if git_entry.is_dir():
            return git_entry
        if git_entry.is_file():
            try:
                raw = git_entry.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            prefix = "gitdir: "
            if raw.startswith(prefix):
                return (git_entry.parent / raw[len(prefix):]).resolve()
            return None
    return None

def current_ref(cwd):
    git_dir = resolve_git_dir(cwd)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return None
    ref = head[5:]
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    if ref.startswith("refs/remotes/"):
        return ref.removeprefix("refs/remotes/")
    return ref

def rejection_reason(argv):
    if not argv:
        return None
    command = argv[0]
    tail = argv[1:]
    if command == "push" and any(arg in {{"--force", "-f", "--force-with-lease", "--mirror"}} for arg in tail):
        return "push with force or mirror is not allowed"
    if command in {{"filter-repo", "filter-branch"}}:
        return f"`git {{command}}` is not allowed"
    if command == "reflog" and tail[:1] == ["expire"]:
        return "`git reflog expire` is not allowed"
    if command == "gc" and any(arg == "--prune=now" or arg.startswith("--prune=now") for arg in tail):
        return "`git gc --prune=now` is not allowed"
    if command == "update-ref" and "-d" in tail:
        for arg in tail:
            if arg.startswith("refs/remotes/"):
                return "deleting remote refs via `git update-ref -d` is not allowed"
    if command == "reset" and "--hard" in tail and any(is_origin_ref(arg) for arg in tail):
        return "`git reset --hard` against origin/* is not allowed"
    if command == "remote" and len(tail) >= 2 and tail[0] == "set-url" and tail[1] == "origin":
        return "`git remote set-url origin` is not allowed"
    ref = current_ref(Path.cwd())
    if command == "rebase":
        if ref is not None and is_protected_ref(ref):
            return "`git rebase` while on a protected ref is not allowed"
        if any(is_protected_ref(arg) for arg in non_option_args(tail)):
            return "`git rebase` onto a protected ref is not allowed"
    if command == "cherry-pick":
        if ref is not None and is_protected_ref(ref):
            return "`git cherry-pick` while on a protected ref is not allowed"
        if any(is_protected_ref(arg) for arg in non_option_args(tail)):
            return "`git cherry-pick` onto a protected ref is not allowed"
    return None

def append_attention_log(workspace, message):
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = Path(workspace) / ".litehive" / "runtime" / "attention.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{{timestamp}}\\t{{message}}\\n")

reason = rejection_reason(sys.argv[1:])
if reason is not None:
    append_attention_log({workspace_root!r}, f"merge-resolver git wrapper rejected `git {{' '.join(sys.argv[1:])}}`: {{reason}}")
    print(f"litehive git wrapper: blocked destructive git command: {{reason}}", file=sys.stderr)
    raise SystemExit(2)
os.execv({real_git_path!r}, [{real_git_path!r}, *sys.argv[1:]])
"""


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
        )

    def render_transcript(self, execution: CLIExecutionResult) -> str:
        return self._adapter.render_transcript(execution)

    def parse_stage_report(
        self,
        *,
        task_id: str,
        step: str,
        execution: CLIExecutionResult,
        subagent_status: str,
    ):
        return self._adapter.parse_stage_report(
            task_id=task_id,
            step=step,
            execution=execution,
            subagent_status=subagent_status,
        )
