"""
Workspace configuration models and their normalizers.

Owns the validation constants, supporting dataclasses
(``ExternalEngineSandboxConfig``, ``ExternalEngineSandboxPolicy``,
``SandboxCredentialInput``), and the primary ``LitehiveConfig``
aggregate. Normalizers run at config-load time so a malformed
workspace ``config.yaml`` fails up front with a precise error
message rather than producing odd behaviour later.
"""

from dataclasses import dataclass, field, fields
import re
from typing import Any, Mapping, Sequence

from pydantic import TypeAdapter, ValidationError
import yaml

from litehive.config.profiles.loader import available_process_profiles
from litehive.domain.common import TransientFailureKind, runner_hook_points
from litehive.domain.roles import agent_startup_guidance_keys


_CONFIG_LIST_ADAPTER = TypeAdapter(list[object])
_CONFIG_MAPPING_ADAPTER = TypeAdapter(dict[object, object])
_RETRY_FAILURE_KIND_ADAPTER = TypeAdapter(TransientFailureKind)


def _represent_transient_failure_kind(dumper, value: TransientFailureKind):
    return dumper.represent_str(value.value)


yaml.add_representer(TransientFailureKind, _represent_transient_failure_kind)
yaml.SafeDumper.add_representer(TransientFailureKind, _represent_transient_failure_kind)


def _config_list(value: object, *, field_name: str) -> list[object]:
    """
    Validate a raw config value as a list.

    Pydantic owns the boundary shape check so strings and scalars
    fail as invalid list inputs before normalizers stringify or
    validate individual elements.
    """
    if value is None:
        return []
    try:
        return _CONFIG_LIST_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"{field_name} must be a list") from exc


def _config_mapping(value: object, *, field_name: str) -> dict[object, object]:
    """
    Validate a raw config value as a mapping.

    Keeps YAML shape validation at the boundary instead of spreading
    ad hoc ``Mapping`` checks through the sandbox config normalizers.
    """
    if value is None:
        return {}
    try:
        return _CONFIG_MAPPING_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"{field_name} must be a mapping") from exc


# --- validation constants ---

VALID_ENGINE_NAMES = frozenset({"codex", "opencode", "gemini", "copilot", "claude", "goz"})
VALID_AGENT_STARTUP_GUIDANCE_KEYS = agent_startup_guidance_keys()
VALID_SANDBOX_NETWORK_MODES = frozenset({"none", "bridge", "host"})
VALID_SANDBOX_WORKSPACE_MODES = frozenset({"ro", "rw"})
VALID_SANDBOX_BACKENDS = frozenset({"docker"})
VALID_RUNNER_HOOK_POINTS = runner_hook_points()
VALID_RUNNER_HOOK_ENTRY_KEYS = frozenset(
    {
        "command",
        "timeout_seconds",
        "description",
        "instructions_on_failure",
    }
)
DEFAULT_SUBAGENT_INACTIVITY_TIMEOUT_SECONDS = 300.0
DEFAULT_TASK_TIME_BUDGET_SECONDS = 3600.0
DEFAULT_DAEMON_HEARTBEAT_INTERVAL_SECONDS = 1.0
DEFAULT_DAEMON_HEALTH_TIMEOUT_SECONDS = 10.0
DEFAULT_DAEMON_STOP_GRACE_PERIOD_SECONDS = 5.0
DEFAULT_DAEMON_FORCE_KILL_TIMEOUT_SECONDS = 4.0
DEFAULT_DAEMON_EXIT_POLL_INTERVAL_SECONDS = 0.1
DEFAULT_DAEMON_STARTUP_TIMEOUT_SECONDS = 5.0
DEFAULT_DAEMON_STARTUP_POLL_INTERVAL_SECONDS = 0.1


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


@dataclass(frozen=True, slots=True)
class ResolvedExternalEngineSandboxPolicy:
    enabled: bool
    network_mode: str
    workspace_mode: str
    environment: tuple[str, ...] = ()
    credential_inputs: tuple[SandboxCredentialInput, ...] = ()
    extra_ro_binds: tuple[str, ...] = ()
    extra_rw_binds: tuple[str, ...] = ()
    setenv: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DaemonConfig:
    heartbeat_interval_seconds: float = DEFAULT_DAEMON_HEARTBEAT_INTERVAL_SECONDS
    health_timeout_seconds: float = DEFAULT_DAEMON_HEALTH_TIMEOUT_SECONDS
    stop_grace_period_seconds: float = DEFAULT_DAEMON_STOP_GRACE_PERIOD_SECONDS
    force_kill_timeout_seconds: float = DEFAULT_DAEMON_FORCE_KILL_TIMEOUT_SECONDS
    exit_poll_interval_seconds: float = DEFAULT_DAEMON_EXIT_POLL_INTERVAL_SECONDS
    startup_timeout_seconds: float = DEFAULT_DAEMON_STARTUP_TIMEOUT_SECONDS
    startup_poll_interval_seconds: float = DEFAULT_DAEMON_STARTUP_POLL_INTERVAL_SECONDS


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

    def policy_for_engine(self, engine_name: str) -> ResolvedExternalEngineSandboxPolicy:
        """
        Resolve the effective sandbox policy for one engine.

        Sandbox enablement is global: when ``enabled`` is true every
        engine gets sandboxed. Per-engine entries only customize the
        environment, credentials, mounts, and optional network or
        workspace mode.
        """
        override = self.engine_policies.get(engine_name)
        if override is None:
            return ResolvedExternalEngineSandboxPolicy(
                enabled=self.enabled,
                network_mode=self.default_network_mode,
                workspace_mode=self.default_workspace_mode,
            )
        return ResolvedExternalEngineSandboxPolicy(
            enabled=self.enabled,
            network_mode=override.network_mode or self.default_network_mode,
            workspace_mode=override.workspace_mode or self.default_workspace_mode,
            environment=tuple(override.environment),
            credential_inputs=tuple(override.credential_inputs),
            extra_ro_binds=tuple(override.extra_ro_binds),
            extra_rw_binds=tuple(override.extra_rw_binds),
            setenv=dict(override.setenv),
        )


# --- primary config ---


@dataclass(slots=True)
class LitehiveConfig:
    """
    Workspace-level configuration aggregate for Litehive.

    Holds the engine selection, retry/budget policy, runner-hook
    map, sandbox spec, and process-profile name as a single
    typed value. Materialized once during config loading and
    threaded through the runtime; mutation is reserved for the
    audited runtime-settings store, not direct attribute writes.
    """

    default_engine: str = "codex"
    recovery_engine: str | None = None
    litehive_source_path: str | None = None
    process_profile: str = "generic"
    opencode_model: str = "zai-coding-plan/glm-5-turbo"
    goz_model: str = "glm-5-turbo"
    gemini_model: str | None = None
    copilot_model: str | None = None
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_turns: int = 100
    default_retry_limit: int = 3
    retry_on: list[TransientFailureKind] = field(
        default_factory=lambda: [
            TransientFailureKind.EXECUTION_LIMIT,
            TransientFailureKind.TIMEOUT,
        ]
    )
    default_stage_retry_limit: int = 2
    default_rejection_loop_limit: int = 3
    pool_stop_on_failure: bool = False
    pool_max_tasks: int | None = None
    pool_stop_on_dirty_git: bool = False
    pool_stop_on_attention: bool = False
    runner_hooks: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    task_time_budget_seconds: float | None = DEFAULT_TASK_TIME_BUDGET_SECONDS
    subagent_inactivity_timeout_seconds: float = DEFAULT_SUBAGENT_INACTIVITY_TIMEOUT_SECONDS
    inactivity_timeout_seconds: float | None = None
    external_engine_sandbox: ExternalEngineSandboxConfig = field(default_factory=ExternalEngineSandboxConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    engine_freeze: dict[str, str] = field(default_factory=dict)
    engine_preference: list[str] = field(default_factory=lambda: ["codex", "opencode", "gemini", "copilot", "goz"])
    agent_startup_guidance: dict[str, list[str]] = field(default_factory=dict)
    auto_commit: bool = True
    task_mode_name: str = "tasks"
    implementation_mode_name: str = "implementation"

    def __post_init__(self) -> None:
        """
        Coerce raw fields into canonical typed shapes after dataclass init.

        Callers that build ``LitehiveConfig(**dict)`` from
        JSON/YAML get the same invariants as code-built instances
        (engine_freeze stringified, engine_preference deduped,
        runner_hooks parsed, retry_on canonicalized). Validates
        numeric bounds for budgets and timeouts so a clearly bad
        value never makes it past load.
        """
        self.engine_freeze = {str(k): str(v) for k, v in self.engine_freeze.items()}
        self.engine_preference = normalize_engine_sequence(
            list(self.engine_preference),
            field_name="engine_preference",
        )
        self.agent_startup_guidance = normalize_agent_startup_guidance(self.agent_startup_guidance)
        self.retry_on = normalize_retry_on(self.retry_on)
        self.runner_hooks = normalize_runner_hooks(self.runner_hooks)
        if self.default_rejection_loop_limit < 1:
            raise ValueError("default_rejection_loop_limit must be greater than 0")
        if self.task_time_budget_seconds is not None:
            self.task_time_budget_seconds = float(self.task_time_budget_seconds)
            if self.task_time_budget_seconds <= 0:
                raise ValueError("task_time_budget_seconds must be greater than 0 when set")
        self.subagent_inactivity_timeout_seconds = float(self.subagent_inactivity_timeout_seconds)
        if self.subagent_inactivity_timeout_seconds <= 0:
            raise ValueError("subagent_inactivity_timeout_seconds must be greater than 0")
        if self.inactivity_timeout_seconds is not None:
            self.inactivity_timeout_seconds = float(self.inactivity_timeout_seconds)
            if self.inactivity_timeout_seconds <= 0:
                raise ValueError("inactivity_timeout_seconds must be greater than 0 when set")
        if self.litehive_source_path is not None:
            self.litehive_source_path = self.litehive_source_path.strip() or None
        self.external_engine_sandbox = normalize_external_engine_sandbox_config(self.external_engine_sandbox)
        self.daemon = normalize_daemon_config(self.daemon)

    def model_for_engine(self, engine_name: str) -> str | None:
        """
        Return the workspace-level default model pinned for an engine.

        The bottom rung of the model-resolution ladder under task-level
        and CLI overrides. Returns ``None`` when the workspace has not
        pinned one so the engine adapter can use its own default.
        """
        if engine_name == "opencode":
            return self.opencode_model
        if engine_name == "goz":
            return self.goz_model
        if engine_name == "gemini":
            return self.gemini_model
        if engine_name == "copilot":
            return self.copilot_model
        if engine_name == "claude":
            return self.claude_model
        return None

    def engine_attempt_order(self, initial_engine_names: list[str]) -> list[str]:
        """
        Build the canonical engine fallback chain for this workspace.

        Concatenates the task's initial engine list with the
        workspace preference. Selection and CLI preview call through
        the config so the same workspace-owned ordering rule is used
        everywhere.
        """
        return list(initial_engine_names) + self.engine_preference


_VALID_CONFIG_KEYS = frozenset(field.name for field in fields(LitehiveConfig))
_LITEHIVE_CONFIG_ADAPTER = TypeAdapter(LitehiveConfig)


def validate_config_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Reject unknown config keys and bad ``process_profile`` values.

    Called before the dict is passed to ``LitehiveConfig(**…)``
    by both the config loader and status diagnostics, so a typo
    in the workspace config fails loudly instead of being
    silently dropped on the floor. Returns the same data
    unmodified on success.
    """
    validated = dict(data)
    profile = validated.get("process_profile")
    available_profiles = available_process_profiles()
    if profile in available_profiles or profile is None:
        pass
    else:
        available_profile_labels = ", ".join(available_profiles)
        raise ValueError(f"unknown process_profile {profile!r}; must be one of: {available_profile_labels}")
    for key in validated:
        if key not in _VALID_CONFIG_KEYS:
            raise ValueError(f"unknown config key {key!r}; remove it or migrate to a supported config field")
    return validated


def parse_litehive_config_data(data: Mapping[str, Any]) -> LitehiveConfig:
    """
    Validate raw config data into the runtime config object.

    This is the single model-owned boundary between YAML/runtime
    dictionaries and the typed ``LitehiveConfig`` used by the rest
    of the process. Unknown-key and process-profile checks still
    run first because they depend on project-specific policy;
    Pydantic owns the dataclass materialization step.
    """
    validated = validate_config_data(data)
    if "runner_hooks" in validated:
        validated["runner_hooks"] = normalize_runner_hooks(validated["runner_hooks"])
    if "external_engine_sandbox" in validated:
        validated["external_engine_sandbox"] = normalize_external_engine_sandbox_config(
            validated["external_engine_sandbox"]
        )
    if "daemon" in validated:
        validated["daemon"] = normalize_daemon_config(validated["daemon"])
    return _LITEHIVE_CONFIG_ADAPTER.validate_python(validated)


def normalize_daemon_config(value: DaemonConfig | Mapping[str, object]) -> DaemonConfig:
    """
    Validate daemon timing values from workspace config.

    Daemon start/stop and heartbeat paths use these values directly
    to decide when a process is stale, when to escalate from SIGTERM
    to SIGKILL, and how long the CLI waits for a background daemon to
    register. Rejecting non-positive values during config load keeps
    those process-control loops from spinning or waiting forever.
    """
    # Config loaders can pass a raw YAML mapping; direct constructors
    # already hold the typed dataclass.
    if isinstance(value, DaemonConfig):
        config = value
    else:
        mapping = _config_mapping(value, field_name="daemon")
        try:
            config = DaemonConfig(**mapping)
        except TypeError as exc:
            raise ValueError(f"invalid daemon config: {exc}") from exc

    config.heartbeat_interval_seconds = _positive_daemon_seconds(
        config.heartbeat_interval_seconds,
        "heartbeat_interval_seconds",
    )
    config.health_timeout_seconds = _positive_daemon_seconds(
        config.health_timeout_seconds,
        "health_timeout_seconds",
    )
    config.stop_grace_period_seconds = _positive_daemon_seconds(
        config.stop_grace_period_seconds,
        "stop_grace_period_seconds",
    )
    config.force_kill_timeout_seconds = _positive_daemon_seconds(
        config.force_kill_timeout_seconds,
        "force_kill_timeout_seconds",
    )
    config.exit_poll_interval_seconds = _positive_daemon_seconds(
        config.exit_poll_interval_seconds,
        "exit_poll_interval_seconds",
    )
    config.startup_timeout_seconds = _positive_daemon_seconds(
        config.startup_timeout_seconds,
        "startup_timeout_seconds",
    )
    config.startup_poll_interval_seconds = _positive_daemon_seconds(
        config.startup_poll_interval_seconds,
        "startup_poll_interval_seconds",
    )
    return config


def _positive_daemon_seconds(value: float, field_name: str) -> float:
    """
    Coerce and validate one daemon timing field.
    """
    seconds = float(value)
    if seconds <= 0:
        raise ValueError(f"daemon.{field_name} must be greater than 0")
    return seconds


def normalize_engine_sequence(engines: Sequence[str], field_name: str) -> list[str]:
    """
    Validate and dedupe an engine list while preserving caller order.

    Rejects unknown engine names with a ``field_name``-tagged
    error so the operator's message points at the specific
    config slot (``engine_preference``, ``runner_hooks``, …)
    rather than a generic "bad engine name". Used wherever engines
    arrive from operator-typed config or runtime-settings.
    """
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
    """
    Normalize the per-role agent startup guidance map.

    Lower-cases role keys, strips whitespace, drops empty
    entries, and rejects role names outside the supported set.
    A typo like ``swee`` fails the config load instead of
    silently shipping no guidance to the agent — the latter
    would be a near-invisible regression.
    """
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
    retry_on: Sequence[str | TransientFailureKind] | None,
    field_name: str = "retry_on",
) -> list[TransientFailureKind]:
    """
    Validate and dedupe the retry-eligible failure-kind list.

    Accepts mixed case from operators but persists lowercase
    canonical kinds so the runner's retry policy never has to
    compare strings ad hoc — the canonical form is what flows
    through the comparison sites in the orchestrator.
    """
    if retry_on is None:
        return []

    normalized: list[TransientFailureKind] = []
    seen: set[TransientFailureKind] = set()
    for raw_kind in retry_on:
        raw_text = str(raw_kind).strip().lower()
        if not raw_text:
            continue
        try:
            kind = _RETRY_FAILURE_KIND_ADAPTER.validate_python(raw_text)
        except ValidationError as exc:
            allowed = ", ".join(kind.value for kind in TransientFailureKind)
            raise ValueError(f"{field_name} must contain only: {allowed}") from exc
        if kind in seen:
            continue
        seen.add(kind)
        normalized.append(kind)
    return normalized


def _normalize_runner_hook(
    raw_hook: str | Mapping[str, object],
    field_name: str,
) -> dict[str, object]:
    """
    Coerce one runner hook entry into a uniform dict shape.

    Accepts the two valid forms — bare command string or full
    mapping — and returns the canonical dict. Rejects unknown
    keys so a typo in a hook-entry field (e.g. ``timout`` vs
    ``timeout_seconds``) surfaces at config load instead of being
    silently dropped at hook-fire time.
    """
    if isinstance(raw_hook, str):
        command = raw_hook.strip()
        if not command:
            raise ValueError(f"{field_name} must not be empty")
        return {"command": command}
    if not isinstance(raw_hook, Mapping):
        raise ValueError(f"{field_name} must be a command string or mapping")

    unknown_keys = sorted(set(raw_hook) - VALID_RUNNER_HOOK_ENTRY_KEYS)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise ValueError(f"{field_name} contains unsupported keys: {joined}")

    command = str(raw_hook.get("command", "")).strip()
    if not command:
        raise ValueError(f"{field_name}.command must not be empty")

    hook: dict[str, object] = {"command": command}
    description = raw_hook.get("description")
    if description is not None:
        cleaned = str(description).strip()
        if cleaned:
            hook["description"] = cleaned
    instructions = raw_hook.get("instructions_on_failure")
    if instructions is not None:
        cleaned = str(instructions).strip()
        if cleaned:
            hook["instructions_on_failure"] = cleaned
    timeout_seconds = raw_hook.get("timeout_seconds")
    if timeout_seconds is not None:
        if not isinstance(timeout_seconds, (int, float, str)):
            raise ValueError(f"{field_name}.timeout_seconds must be a number")
        timeout_value = float(timeout_seconds)
        if timeout_value <= 0:
            raise ValueError(f"{field_name}.timeout_seconds must be greater than 0")
        hook["timeout_seconds"] = timeout_value
    return hook


def normalize_runner_hooks(
    raw_hooks: Mapping[str, Sequence[str | Mapping[str, object]]] | None,
) -> dict[str, list[dict[str, object]]]:
    """
    Validate and normalize the runner-hook map.

    Validates each hook point name and normalizes each entry
    into its dict form. Called from
    ``LitehiveConfig.__post_init__`` so config-supplied hooks
    fail fast (unknown points, missing commands) rather than
    silently being skipped at runtime — a missing hook would be
    nearly invisible in operator-facing output.
    """
    if raw_hooks is None:
        return {}

    normalized: dict[str, list[dict[str, object]]] = {}
    for point, hooks in raw_hooks.items():
        if point not in VALID_RUNNER_HOOK_POINTS:
            allowed = ", ".join(sorted(VALID_RUNNER_HOOK_POINTS))
            raise ValueError(f"runner_hooks key must be one of: {allowed}")
        if not hooks:
            continue
        normalized[point] = _normalize_runner_hook_list(point, hooks)
    return normalized


def _normalize_runner_hook_list(
    point: str,
    hooks,
) -> list[dict[str, object]]:
    """
    Normalize each hook entry under one runner-hook point.

    Threads ``point`` and the per-entry index into the field path
    so a malformed hook surfaces as
    ``runner_hooks[after_implementing][2]`` instead of an opaque
    error. Caller: :func:`_normalize_runner_hooks`.
    """
    normalized: list[dict[str, object]] = []
    for index, hook in enumerate(hooks):
        normalized.append(
            _normalize_runner_hook(
                hook,
                field_name=f"runner_hooks[{point}][{index}]",
            )
        )
    return normalized


def _normalize_sandbox_credential_input(
    raw_input: object,
    field_name: str,
) -> SandboxCredentialInput:
    """
    Coerce a sandbox credential-input entry and validate its shape.

    Enforces uppercase env-var names and absolute container
    mount paths so a bad credential entry fails the config load
    rather than leaking a malformed ``docker -e`` argument at
    sandbox launch — that failure would otherwise show up as an
    obscure docker error without context.
    """
    if isinstance(raw_input, SandboxCredentialInput):
        credential = raw_input
    elif isinstance(raw_input, Mapping):
        env_var = str(raw_input.get("env_var", "")).strip()
        mount_path = str(raw_input.get("mount_path", "")).strip()
        credential = SandboxCredentialInput(env_var=env_var, mount_path=mount_path)
    else:
        raise ValueError(f"{field_name} must be a mapping or SandboxCredentialInput")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", credential.env_var):
        raise ValueError(f"{field_name}.env_var must be an uppercase environment variable name")
    if not credential.mount_path.startswith("/"):
        raise ValueError(f"{field_name}.mount_path must be an absolute container path")
    return credential


def _normalize_bind_list(raw_binds: list[str], field_name: str) -> list[str]:
    """
    Strip and validate a sandbox bind-mount list.

    Requires absolute host paths so the docker ``-v`` flag never
    receives a relative path; docker would silently treat such a
    path as a named volume and the bind would not behave as
    intended. Drops blank entries to tolerate operator-typed
    config with stray empty lines.
    """
    normalized: list[str] = []
    for index, raw_path in enumerate(raw_binds):
        host_path = raw_path.strip()
        if not host_path:
            continue
        if not host_path.startswith("/"):
            raise ValueError(f"{field_name}[{index}] must be an absolute host path")
        normalized.append(host_path)
    return normalized


def _normalize_sandbox_credential_inputs(
    raw_inputs: object,
    field_name: str,
) -> list[SandboxCredentialInput]:
    """
    Normalize each entry of a sandbox ``credential_inputs`` list.

    Iterates the operator-supplied iterable, threading per-index
    field paths so a malformed entry surfaces as
    ``credential_inputs[2].env_var ...`` rather than a flat error.
    Caller: :func:`_normalize_external_engine_sandbox_policy`.
    """
    iterable = _config_list(raw_inputs, field_name=field_name)
    credentials: list[SandboxCredentialInput] = []
    for index, item in enumerate(iterable):
        credential = _normalize_sandbox_credential_input(
            item,
            field_name=f"{field_name}[{index}]",
        )
        credentials.append(credential)
    return credentials


def _stripped_bind_strings(raw_binds: object, field_name: str) -> list[str]:
    """
    Coerce a sandbox bind list to stripped strings.

    Pre-pass for :func:`_normalize_bind_list`: the operator's YAML
    can mix ints, paths, and stray whitespace, so each entry is
    stringified and trimmed before later validation enforces
    absolute-path rules. Caller:
    :func:`_normalize_external_engine_sandbox_policy`.
    """
    iterable = _config_list(raw_binds, field_name=field_name)
    binds: list[str] = []
    for item in iterable:
        binds.append(str(item).strip())
    return binds


def _stringify_setenv_mapping(raw_setenv: object, field_name: str) -> dict[str, str]:
    """
    Stringify both keys and values of a sandbox ``setenv`` map.

    Operator config may type either as numeric or non-string values
    (e.g. an integer port). The downstream env-name regex check
    needs a string key, and docker's ``-e`` accepts only string
    values. Caller:
    :func:`_normalize_external_engine_sandbox_policy`.
    """
    mapping = _config_mapping(raw_setenv, field_name=field_name)
    setenv: dict[str, str] = {}
    for key, value in mapping.items():
        setenv[str(key)] = str(value)
    return setenv


def _normalize_external_engine_sandbox_policy(
    raw_policy: object,
    field_name: str,
) -> ExternalEngineSandboxPolicy:
    """
    Build a per-engine sandbox policy and re-validate its fields.

    Accepts a typed policy or a raw mapping. Validates uppercase
    env names, allowed network/workspace modes, and absolute
    bind paths so each engine's sandbox spec is checked
    independently before docker sees it; a bad value caught here
    is far cheaper to diagnose than a sandbox launch failure.
    """
    if isinstance(raw_policy, ExternalEngineSandboxPolicy):
        policy = raw_policy
    elif isinstance(raw_policy, Mapping):
        if raw_policy.get("network_mode") is None:
            network_mode_arg = None
        else:
            network_mode_arg = str(raw_policy.get("network_mode"))
        if raw_policy.get("workspace_mode") is None:
            workspace_mode_arg = None
        else:
            workspace_mode_arg = str(raw_policy.get("workspace_mode"))
        policy = ExternalEngineSandboxPolicy(
            enabled=bool(raw_policy.get("enabled", False)),
            network_mode=network_mode_arg,
            workspace_mode=workspace_mode_arg,
            environment=[
                str(item)
                for item in _config_list(
                    raw_policy.get("environment"),
                    field_name=f"{field_name}.environment",
                )
            ],
            credential_inputs=_normalize_sandbox_credential_inputs(
                raw_policy.get("credential_inputs"),
                field_name=f"{field_name}.credential_inputs",
            ),
            extra_ro_binds=_stripped_bind_strings(
                raw_policy.get("extra_ro_binds"),
                field_name=f"{field_name}.extra_ro_binds",
            ),
            extra_rw_binds=_stripped_bind_strings(
                raw_policy.get("extra_rw_binds"),
                field_name=f"{field_name}.extra_rw_binds",
            ),
            setenv=_stringify_setenv_mapping(
                raw_policy.get("setenv"),
                field_name=f"{field_name}.setenv",
            ),
        )
    else:
        raise ValueError(f"{field_name} must be a mapping or ExternalEngineSandboxPolicy")
    for index, env_name in enumerate(policy.environment):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise ValueError(f"{field_name}.environment[{index}] must be an uppercase environment variable name")
    for env_name in policy.setenv.keys():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise ValueError(f"{field_name}.setenv key {env_name!r} must be an uppercase environment variable name")
    if policy.network_mode is not None and policy.network_mode not in VALID_SANDBOX_NETWORK_MODES:
        allowed = ", ".join(sorted(VALID_SANDBOX_NETWORK_MODES))
        raise ValueError(f"{field_name}.network_mode must be one of: {allowed}")
    if policy.workspace_mode is not None and policy.workspace_mode not in VALID_SANDBOX_WORKSPACE_MODES:
        allowed = ", ".join(sorted(VALID_SANDBOX_WORKSPACE_MODES))
        raise ValueError(f"{field_name}.workspace_mode must be one of: {allowed}")
    policy.extra_ro_binds = _normalize_bind_list(policy.extra_ro_binds, field_name=f"{field_name}.extra_ro_binds")
    policy.extra_rw_binds = _normalize_bind_list(policy.extra_rw_binds, field_name=f"{field_name}.extra_rw_binds")
    return policy


def _normalize_engine_policies_map(
    raw_engine_policies: object,
) -> dict[str, ExternalEngineSandboxPolicy]:
    """
    Normalize the per-engine sandbox-policy mapping.

    Ensures every engine-name key is a string and every value is
    a fully-validated :class:`ExternalEngineSandboxPolicy`. Threads
    the engine-name into the field path so a malformed entry shows
    up as ``engine_policies[claude].extra_ro_binds`` rather than a
    flat error. Caller:
    :func:`normalize_external_engine_sandbox_config`.
    """
    mapping = _config_mapping(
        raw_engine_policies,
        field_name="external_engine_sandbox.engine_policies",
    )
    policies: dict[str, ExternalEngineSandboxPolicy] = {}
    for engine_name, policy in mapping.items():
        engine_key = str(engine_name)
        policies[engine_key] = _normalize_external_engine_sandbox_policy(
            policy,
            field_name=f"external_engine_sandbox.engine_policies[{engine_key}]",
        )
    return policies


def normalize_external_engine_sandbox_config(
    raw_config: ExternalEngineSandboxConfig | Mapping[str, object] | None,
) -> ExternalEngineSandboxConfig:
    """
    Coerce a sandbox-config mapping into the typed config dataclass.

    Validates backend, default network/workspace modes, and
    absolute mount paths so a half-formed sandbox spec never
    reaches docker at runtime. Re-runs the per-engine policy
    normalizer for every entry under ``engine_policies`` so
    typed and raw-mapping inputs end up with the same checks.
    """
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
            runtime_args=[
                str(item)
                for item in _config_list(
                    raw_config.get("runtime_args"),
                    field_name="external_engine_sandbox.runtime_args",
                )
            ],
            default_network_mode=str(raw_config.get("default_network_mode", "none")),
            default_workspace_mode=str(raw_config.get("default_workspace_mode", "rw")),
            read_only_rootfs=bool(raw_config.get("read_only_rootfs", True)),
            drop_capabilities=bool(raw_config.get("drop_capabilities", True)),
            no_new_privileges=bool(raw_config.get("no_new_privileges", True)),
            tmpfs=[
                str(item)
                for item in _config_list(
                    raw_config.get("tmpfs", ["/tmp"]),
                    field_name="external_engine_sandbox.tmpfs",
                )
            ],
            engine_policies=_normalize_engine_policies_map(raw_config.get("engine_policies")),
        )
    if config.backend not in VALID_SANDBOX_BACKENDS:
        allowed = ", ".join(sorted(VALID_SANDBOX_BACKENDS))
        raise ValueError(f"external_engine_sandbox.backend must be one of: {allowed}")
    if config.default_network_mode not in VALID_SANDBOX_NETWORK_MODES:
        allowed = ", ".join(sorted(VALID_SANDBOX_NETWORK_MODES))
        raise ValueError(f"external_engine_sandbox.default_network_mode must be one of: {allowed}")
    if config.default_workspace_mode not in VALID_SANDBOX_WORKSPACE_MODES:
        allowed = ", ".join(sorted(VALID_SANDBOX_WORKSPACE_MODES))
        raise ValueError(f"external_engine_sandbox.default_workspace_mode must be one of: {allowed}")
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
            raise ValueError(f"external_engine_sandbox.engine_policies key must be one of: {allowed}")
        normalized_policies[engine_name] = _normalize_external_engine_sandbox_policy(
            policy,
            field_name=f"external_engine_sandbox.engine_policies[{engine_name}]",
        )
    config.engine_policies = normalized_policies
    config.tmpfs = [mount_path for mount_path in config.tmpfs if mount_path.strip()]
    return config
