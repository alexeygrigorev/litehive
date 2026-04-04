"""Workspace configuration, process profiles, and bootstrap helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import yaml


VALID_POOL_SELECTION_POLICIES = {"fifo", "priority_first", "dependency_aware"}
VALID_ENGINE_NAMES = frozenset({"codex", "opencode", "gemini", "copilot", "claude", "goz"})
VALID_AGENT_STARTUP_GUIDANCE_KEYS = frozenset({"all", "planner", "swe", "qa", "reviewer", "recovery"})
VALID_EXECUTION_RETRY_SELECTORS = frozenset({*VALID_ENGINE_NAMES, "external_cli"})
VALID_EXECUTION_RETRY_CLASSIFICATIONS = frozenset({"timeout", "network", "service"})
VALID_SANDBOX_NETWORK_MODES = frozenset({"none", "bridge", "host"})
VALID_SANDBOX_WORKSPACE_MODES = frozenset({"ro", "rw"})
VALID_SANDBOX_BACKENDS = frozenset({"docker", "bubblewrap"})
VALID_RUNNER_HOOK_POINTS = frozenset(
    {
        "before_swe_implementation",
        "after_swe_implementation",
        "before_pm_acceptance",
        "after_pm_acceptance",
    }
)
MODEL_FAMILY_RETRY_SELECTOR_PREFIX = "model_family:"
ENGINE_CATEGORY_RETRY_SELECTOR_PREFIX = "engine_category:"


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


def _default_task_engine_routing() -> dict[str, list[str]]:
    return {
        "adapter": ["codex", "opencode", "gemini", "copilot", "goz"],
        "bugfix": ["codex", "opencode", "copilot", "gemini", "goz"],
        "research": ["gemini", "codex", "opencode", "copilot", "goz"],
        "review": ["copilot", "codex", "opencode", "gemini", "goz"],
        "refactor": ["opencode", "codex", "copilot", "gemini", "goz"],
        "docs": ["codex", "gemini", "opencode", "copilot", "goz"],
        "intake": ["opencode", "codex", "gemini", "copilot", "goz"],
    }


VALID_TASK_ROUTING_KEYS = frozenset(_default_task_engine_routing())


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


def normalize_task_engine_routing(
    routing: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    normalized = _default_task_engine_routing()
    if routing is None:
        return normalized

    for route_key, engines in routing.items():
        if route_key not in VALID_TASK_ROUTING_KEYS:
            allowed = ", ".join(sorted(VALID_TASK_ROUTING_KEYS))
            raise ValueError(f"task_engine_routing key must be one of: {allowed}")
        normalized[route_key] = _normalize_engine_sequence(
            list(engines),
            field_name=f"task_engine_routing[{route_key}]",
        )
    return normalized


@dataclass(slots=True)
class ExecutionRetryPolicy:
    max_retries: int = 0
    backoff_seconds: float = 0.0
    backoff_multiplier: float = 1.0
    retry_on: list[str] = field(
        default_factory=lambda: ["timeout", "network", "service"]
    )


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
    blocking: bool = True


def _normalize_runner_hook_config(
    raw_hook: RunnerHookConfig | Mapping[str, object],
    *,
    field_name: str,
) -> RunnerHookConfig:
    hook = raw_hook if isinstance(raw_hook, RunnerHookConfig) else RunnerHookConfig(**dict(raw_hook))
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
                None if raw_limits.get("process_limit") is None else int(raw_limits.get("process_limit"))
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
                None if raw_policy.get("network_mode") is None else str(raw_policy.get("network_mode"))
            ),
            workspace_mode=(
                None if raw_policy.get("workspace_mode") is None else str(raw_policy.get("workspace_mode"))
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
    if policy.workspace_mode is not None and policy.workspace_mode not in VALID_SANDBOX_WORKSPACE_MODES:
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
            runtime_binary=str(raw_config.get("runtime_binary", "bwrap" if backend == "bubblewrap" else "docker")),
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


def _default_execution_retry_policies() -> dict[str, ExecutionRetryPolicy]:
    return {
        "claude": ExecutionRetryPolicy(
            max_retries=2,
            backoff_seconds=0.25,
            backoff_multiplier=2.0,
            retry_on=["timeout", "network", "service"],
        ),
        "codex": ExecutionRetryPolicy(
            max_retries=2,
            backoff_seconds=0.25,
            backoff_multiplier=2.0,
            retry_on=["timeout", "network", "service"],
        ),
        "opencode": ExecutionRetryPolicy(
            max_retries=2,
            backoff_seconds=0.25,
            backoff_multiplier=2.0,
            retry_on=["timeout", "network", "service"],
        ),
        "gemini": ExecutionRetryPolicy(
            max_retries=2,
            backoff_seconds=0.25,
            backoff_multiplier=2.0,
            retry_on=["timeout", "network", "service"],
        ),
        "goz": ExecutionRetryPolicy(
            max_retries=2,
            backoff_seconds=0.25,
            backoff_multiplier=2.0,
            retry_on=["timeout", "network", "service"],
        ),
    }


def _shared_stage_sequence() -> list[str]:
    return ["grooming", "implementing", "testing", "accepting", "commit_to_git"]


def _shared_stage_text(stages: list[str]) -> str:
    return " -> ".join(stages) + "."


SHARED_PROCESS_PROFILE: dict[str, Any] = {
    "label": "Generic",
    "summary": "General software project workflow with deterministic local orchestration.",
    "source_of_truth": "tasks and implementation state live under `.litehive/`.",
    "task_source_of_truth": "issues or task records define scope; prompts and transcripts are supporting evidence.",
    "orchestrator_model": "the local runner is the manager and owns stage routing.",
    "routing_model": "routing stays deterministic and local; subagents execute assigned stages but do not self-route.",
    "shared_stages": _shared_stage_sequence(),
    "role_model": (
        "orchestrator-as-manager: `planner` owns grooming, `reviewer` owns acceptance, "
        "both are PM-style product roles, `swe` implements, and `qa` verifies."
    ),
    "tdd_expectations": "prefer test-first or test-tight changes and explain deviations.",
    "verification_discipline": "verification should be explicit, focused, and independent enough to catch regressions.",
    "acceptance_flow": "implementation must pass verification before acceptance.",
    "commit_recovery": (
        "successful tasks checkpoint to git; rollback and recover should remain deterministic."
    ),
    "prompt_scaffold": [
        "- Start from the shared process contract, then add repository context and task data.",
        "- Combine the generic base prompt with the selected project overlay instead of replacing the base.",
        "- Apply stage defaults first, then append any project-specific stage overlay for that step.",
        "- Keep stage prompts explicit about role, verification expectations, and final report format.",
    ],
    "init_scaffold": [
        "- Scaffold `.litehive/context.md` from the generic base process template.",
        "- Layer the project profile summary, workspace overlay, and stage overlay onto that base.",
        "- Treat process profiles as overlays on the shared contract rather than separate workflows.",
        "- Keep the task/issue source of truth, verification commands, and recovery policy visible in the scaffold.",
    ],
    "development_rules": [
        "- Keep changes scoped to the current task.",
        "- Prefer targeted tests over broad test suites.",
        "- Record assumptions clearly in the final report.",
    ],
    "tool_usage": [
        "- Use `uv run pytest -q` for the current smoke test suite.",
        "- Update litehive task artifacts instead of inventing external state stores.",
        "- If you add a new command or workflow, document it here for future runs.",
    ],
    "workspace_overlay": [
        "- Favor incremental, reviewable changes over broad refactors.",
        "- Keep implementation, verification, and acceptance evidence explicit.",
    ],
    "stage_instructions": {
        "grooming": [
            "Act as the planner: clarify the user problem, inspect the repo if needed, and produce a concrete execution plan.",
            "Focus on scope clarification, acceptance criteria quality, decomposition, follow-up tasks, and PM sizing.",
            "Do not make code changes in this stage.",
        ],
        "implementing": [
            "Implement the task in this repository.",
            "Keep changes tightly scoped and complete the work needed for the acceptance criteria.",
        ],
        "testing": [
            "Validate the implementation.",
            "Run focused checks or tests where possible and report failures precisely.",
            "Only make minimal fixes if absolutely necessary.",
        ],
        "accepting": [
            "Act as the reviewer: validate the end-user outcome against the acceptance criteria and decide whether it should be accepted or sent back.",
            "Be strict about regression detection, evidence quality, and final done versus not-done judgment.",
        ],
    },
    "stage_overlay": {},
    "specifics_heading": None,
    "specifics": [],
}


PROCESS_PROFILES: dict[str, dict[str, Any]] = {
    "generic": {},
    "python": {
        "label": "Python",
        "summary": "Python package or application workflow with pytest-oriented verification.",
        "role_model": (
            "`planner` frames the task, `reviewer` performs final PM-style acceptance, "
            "`swe` edits code, and `qa` runs focused verification."
        ),
        "tdd_expectations": "add or update focused tests near the changed Python module before broad suites.",
        "verification_discipline": (
            "prefer targeted `pytest` evidence close to the changed module before broader smoke coverage."
        ),
        "acceptance_flow": "verify behavior with targeted `pytest` coverage and note any residual risk.",
        "commit_recovery": "keep checkpoint commits deterministic and easy to recover.",
        "specifics_heading": "## Python specifics",
        "specifics": [
            "- Prefer `pytest` for automated verification.",
            "- Keep module boundaries and import hygiene clear.",
            "- Record virtualenv, `uv`, or toolchain expectations when they matter.",
        ],
        "workspace_overlay": [
            "- Prefer focused `pytest` coverage for the changed modules.",
            "- Keep dependency and packaging changes explicit and minimal.",
        ],
        "init_scaffold": [
            "- Seed Python workspaces with package layout, test entrypoints, and `uv` or virtualenv expectations.",
        ],
        "stage_overlay": {
            "implementing": [
                "- Write or update focused tests alongside the code change when feasible.",
            ],
            "testing": [
                "- Prefer targeted `pytest` invocations before broader test commands.",
            ],
        },
    },
    "django": {
        "label": "Django",
        "summary": "Django application workflow with app-level tests, migrations discipline, and settings awareness.",
        "role_model": (
            "`planner` frames the task, `reviewer` performs final PM-style acceptance, "
            "`swe` updates apps/views/models, and `qa` verifies user-visible behavior."
        ),
        "tdd_expectations": (
            "prefer app-level or regression tests first, especially around models, views, forms, and APIs."
        ),
        "verification_discipline": (
            "verify request paths, ORM behavior, and migration impact with focused Django or pytest coverage."
        ),
        "acceptance_flow": "confirm migrations, settings impact, and targeted test coverage before acceptance.",
        "commit_recovery": "keep schema and data-shape changes explicit so rollback remains predictable.",
        "specifics_heading": "## Django specifics",
        "specifics": [
            "- Call out migrations, settings, fixtures, and management command impacts.",
            "- Prefer focused test modules or app test cases over full-project runs when possible.",
            "- Note any admin, template, or ORM behavior affected by the task.",
        ],
        "tool_usage": [
            "- Use `uv run pytest -q` for the current smoke test suite unless the repo documents a different Django test command.",
            "- Update litehive task artifacts instead of inventing external state stores.",
            "- If you add a new command or workflow, document it here for future runs.",
        ],
        "workspace_overlay": [
            "- Treat migrations, settings, and database state as first-class review items.",
            "- Prefer narrow regression tests around the affected Django app and request path.",
        ],
        "init_scaffold": [
            "- Seed Django workspaces with app boundaries, settings entrypoints, and migration review notes.",
        ],
        "stage_overlay": {
            "implementing": [
                "- Keep model, migration, view, form, and template changes coordinated.",
            ],
            "testing": [
                "- Verify migrations and run focused Django or pytest coverage for the affected app.",
            ],
            "accepting": [
                "- Reject changes that leave migration or settings impact unclear.",
            ],
        },
    },
    "rust": {
        "label": "Rust",
        "summary": "Rust crate or workspace workflow with compile-first discipline and tight tests.",
        "role_model": (
            "`planner` scopes the task, `reviewer` performs final PM-style acceptance, "
            "`swe` edits crates/modules, and `qa` verifies compile and test results."
        ),
        "tdd_expectations": "prefer regression tests or unit tests close to the affected module before broader runs.",
        "verification_discipline": "treat `cargo check`, focused tests, and compiler warnings as acceptance evidence.",
        "acceptance_flow": "compile, run focused tests, and surface warnings or clippy debt explicitly.",
        "commit_recovery": "keep checkpoints deterministic and avoid opaque generated churn.",
        "tool_usage": [
            "- Use the repository's documented `cargo` commands for targeted verification.",
            "- Update litehive task artifacts instead of inventing external state stores.",
            "- If you add a new command or workflow, document it here for future runs.",
        ],
        "specifics_heading": "## Rust specifics",
        "specifics": [
            "- Prefer `cargo test` and targeted package/module verification.",
            "- Keep ownership, error handling, and public API changes explicit.",
            "- Note formatting, lint, and feature-flag expectations when relevant.",
        ],
        "workspace_overlay": [
            "- Favor small, compile-safe changes with clear module ownership.",
            "- Treat compiler errors, warnings, and feature flags as part of verification evidence.",
        ],
        "init_scaffold": [
            "- Seed Rust workspaces with crate boundaries, toolchain expectations, and `cargo` verification commands.",
        ],
        "stage_overlay": {
            "implementing": [
                "- Add or adjust focused Rust tests close to the changed crate or module.",
            ],
            "testing": [
                "- Prefer targeted `cargo test`, `cargo check`, or package-scoped verification before workspace-wide runs.",
            ],
        },
    },
    "cpp": {
        "label": "C/C++",
        "summary": "C or C++ workflow with compile-heavy verification, native toolchains, and linker-aware debugging.",
        "role_model": (
            "`planner` scopes the change, `reviewer` performs final PM-style acceptance, "
            "`swe` edits native code, and `qa` verifies compile and runtime behavior."
        ),
        "tdd_expectations": (
            "prefer focused native regression coverage near the affected target before broader rebuilds."
        ),
        "verification_discipline": (
            "treat compile success, targeted tests, and linker/toolchain diagnostics as acceptance evidence."
        ),
        "acceptance_flow": "verify the target builds cleanly, run focused checks, and surface warnings explicitly.",
        "commit_recovery": "keep generated build churn out of scope so checkpoints stay reviewable and deterministic.",
        "tool_usage": [
            "- Use the repository's documented build and test commands for the affected native target.",
            "- Update litehive task artifacts instead of inventing external state stores.",
            "- If you add a new command or workflow, document it here for future runs.",
        ],
        "specifics_heading": "## C/C++ specifics",
        "specifics": [
            "- Prefer target-scoped builds and tests over full rebuilds when possible.",
            "- Keep ABI, toolchain, warning, and linker impacts explicit.",
            "- Record sanitizer, compiler, or build-system expectations when they affect verification.",
        ],
        "workspace_overlay": [
            "- Favor narrow changes that keep compile and linker failures easy to localize.",
            "- Treat toolchain warnings, generated artifacts, and native resource usage as first-class signals.",
        ],
        "init_scaffold": [
            "- Seed C/C++ workspaces with target boundaries, build commands, and toolchain expectations.",
        ],
        "stage_overlay": {
            "implementing": [
                "- Keep native build-system, header, and source changes coordinated and reviewable.",
            ],
            "testing": [
                "- Prefer target-scoped compile and test commands before broader native builds.",
            ],
        },
    },
    "codehive": {
        "label": "Codehive-style",
        "summary": "Multi-agent coding workflow emphasizing manager routing, TDD, and deterministic recovery.",
        "source_of_truth": "issue/task state and execution artifacts must stay local and explicit.",
        "task_source_of_truth": (
            "issues or queued task records stay authoritative; subagent work must map back to that source."
        ),
        "orchestrator_model": "the orchestrator is the manager; subagents execute but do not choose routing.",
        "routing_model": "manager-owned deterministic routing, retries, and escalation stay in local code rather than prompts.",
        "role_model": (
            "`planner` owns task shaping, `reviewer` owns final PM-style acceptance, "
            "`swe` implements, and `qa` verifies, all through deterministic stage handoffs."
        ),
        "tdd_expectations": "default to regression-first or test-first implementation and explain any exception.",
        "verification_discipline": (
            "verification should be independent enough to catch behavioral regressions, not just restate implementation intent."
        ),
        "acceptance_flow": "only accept after focused verification and explicit comparison against task acceptance.",
        "commit_recovery": (
            "accepted tasks commit by default at commit_to_git using "
            "`litehive: complete <task-id> <task-slug>`; reruns append `(attempt N)`, "
            "rollback reverts that checkpoint into a new rollback commit, and recover requeues without reverting code."
        ),
        "specifics_heading": "## Codehive-style specifics",
        "specifics": [
            "- Preserve execution visibility through task reports, subagent transcripts, and recent progress.",
            "- Prefer narrow, reviewable scope per task and push follow-up work into later tasks.",
            "- Keep routing, retries, and escalation in local code rather than prompt-only behavior.",
        ],
        "tool_usage": [
            "- Update litehive task artifacts instead of inventing external state stores.",
            "- If you add a new workflow or command, document it here so future runs inherit the same context.",
        ],
        "workspace_overlay": [
            "- Treat the orchestrator as the manager and the only authority for routing and retries.",
            "- Maintain high execution visibility through reports, transcripts, and explicit stage summaries.",
        ],
        "init_scaffold": [
            "- Seed codehive-style workspaces with deterministic routing notes, visibility expectations, and checkpoint policy.",
        ],
        "stage_overlay": {
            "grooming": [
                "- Break ambiguity down into a concrete, deterministic plan with clear ownership.",
                "- Planner output should sharpen user value, scope edges, decomposition, and follow-up work.",
            ],
            "implementing": [
                "- Stay within the current task boundary and prefer test-first changes when the repo supports it.",
            ],
            "testing": [
                "- Verification should be independent enough to catch behavioral regressions, not just restate implementation intent.",
            ],
            "accepting": [
                "- Reviewer acceptance is managerial PM-style review against task goals, tests, and recovery policy, not a rubber stamp.",
                "- Reviewer judgment should focus on end-user outcome, regressions, and whether the task is actually done.",
                "- Accepted tasks proceed to `commit_to_git`, where Litehive creates the final checkpoint commit unless auto-commit is explicitly disabled.",
            ],
        },
    },
}


@dataclass(slots=True)
class LitehiveConfig:
    default_engine: str = "codex"
    recovery_engine: str | None = None
    process_profile: str = "generic"
    codex_model: str | None = None
    opencode_model: str = "zai-coding-plan/glm-5.1"
    goz_model: str = "glm-5-turbo"
    gemini_model: str | None = None
    copilot_model: str | None = None
    claude_enabled: bool = False
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_turns: int = 100
    pool_usage_cap: int | None = None
    pool_cost_cap: int | None = None
    engine_usage_caps: dict[str, int] = field(default_factory=dict)
    engine_budget_caps: dict[str, int] = field(default_factory=dict)
    engine_costs: dict[str, int] = field(
        default_factory=lambda: {
            "codex": 1,
            "opencode": 1,
            "gemini": 1,
            "copilot": 1,
            "claude": 3,
            "goz": 1,
        }
    )
    default_retry_limit: int = 3
    execution_retry_policies: dict[str, ExecutionRetryPolicy] = field(
        default_factory=_default_execution_retry_policies
    )
    pool_stop_on_failure: bool = False
    pool_max_tasks: int | None = None
    pool_stop_on_execution_limit: bool = False
    pool_quota_threshold: int | None = None
    pool_budget_threshold: int | None = None
    pool_stop_on_dirty_git: bool = False
    pool_selection_policy: str = "dependency_aware"
    pre_acceptance_command: str | None = None
    runner_hooks: dict[str, list[RunnerHookConfig]] = field(default_factory=dict)
    subagent_resource_limits: SubagentResourceLimitsConfig = field(
        default_factory=SubagentResourceLimitsConfig
    )
    external_engine_sandbox: ExternalEngineSandboxConfig = field(
        default_factory=ExternalEngineSandboxConfig
    )
    task_engine_routing: dict[str, list[str]] = field(default_factory=_default_task_engine_routing)
    engine_fallbacks: dict[str, list[str]] = field(
        default_factory=lambda: {
            "codex": ["opencode", "gemini", "copilot"],
            "opencode": ["codex", "gemini", "copilot"],
            "gemini": ["codex", "opencode", "copilot"],
            "copilot": ["codex", "opencode", "gemini"],
            "goz": ["opencode", "codex", "gemini", "copilot"],
        }
    )
    agent_startup_guidance: dict[str, list[str]] = field(default_factory=dict)
    auto_commit: bool = True
    task_mode_name: str = "tasks"
    implementation_mode_name: str = "implementation"

    def __post_init__(self) -> None:
        self.task_engine_routing = normalize_task_engine_routing(self.task_engine_routing)
        self.engine_usage_caps = _normalize_engine_int_map(
            self.engine_usage_caps,
            field_name="engine_usage_caps",
        )
        self.engine_budget_caps = _normalize_engine_int_map(
            self.engine_budget_caps,
            field_name="engine_budget_caps",
        )
        self.engine_costs = _normalize_engine_int_map(
            self.engine_costs,
            field_name="engine_costs",
        )
        self.engine_fallbacks = {
            engine_name: _normalize_engine_sequence(
                list(fallbacks),
                field_name=f"engine_fallbacks[{engine_name}]",
            )
            for engine_name, fallbacks in self.engine_fallbacks.items()
        }
        self.agent_startup_guidance = _normalize_agent_startup_guidance(
            self.agent_startup_guidance
        )
        self.execution_retry_policies = _normalize_execution_retry_policies(
            self.execution_retry_policies
        )
        self.runner_hooks = _normalize_runner_hooks(self.runner_hooks)
        if self.pre_acceptance_command is not None:
            self.pre_acceptance_command = self.pre_acceptance_command.strip() or None
        if self.pre_acceptance_command:
            before_acceptance_hooks = self.runner_hooks.setdefault("before_pm_acceptance", [])
            matched_legacy_hook = False
            for hook in before_acceptance_hooks:
                if hook.command == self.pre_acceptance_command:
                    hook.blocking = True
                    matched_legacy_hook = True
            if not matched_legacy_hook:
                before_acceptance_hooks.append(
                    RunnerHookConfig(command=self.pre_acceptance_command, blocking=True)
                )
        self.subagent_resource_limits = _normalize_subagent_resource_limits(
            self.subagent_resource_limits,
            process_profile=self.process_profile,
        )
        self.external_engine_sandbox = _normalize_external_engine_sandbox_config(
            self.external_engine_sandbox
        )


def workspace_dir(root: Path) -> Path:
    return root / ".litehive"


def config_path(root: Path) -> Path:
    return workspace_dir(root) / "config.yaml"


def global_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "litehive" / "config.yaml"
    return Path.home() / ".config" / "litehive" / "config.yaml"


def state_path(root: Path) -> Path:
    return workspace_dir(root) / "state.yaml"


def context_path(root: Path) -> Path:
    return workspace_dir(root) / "context.md"


def workspace_gitignore_path(root: Path) -> Path:
    return workspace_dir(root) / ".gitignore"


def available_process_profiles() -> list[str]:
    return sorted(PROCESS_PROFILES)


def resolve_process_profile(name: str | None) -> dict[str, Any]:
    profile = deepcopy(SHARED_PROCESS_PROFILE)
    if name is None:
        return profile

    overlay = PROCESS_PROFILES.get(name, PROCESS_PROFILES["generic"])
    for key, value in overlay.items():
        if key in {
            "development_rules",
            "tool_usage",
            "workspace_overlay",
            "specifics",
            "prompt_scaffold",
            "init_scaffold",
        }:
            profile[key].extend(deepcopy(value))
            continue
        if key == "stage_overlay":
            for stage, instructions in value.items():
                profile["stage_overlay"].setdefault(stage, []).extend(deepcopy(instructions))
            continue
        if key == "stage_instructions":
            for stage, instructions in value.items():
                profile["stage_instructions"].setdefault(stage, []).extend(deepcopy(instructions))
            continue
        profile[key] = deepcopy(value)
    return profile


def _render_process_overlay(profile: dict[str, Any]) -> list[str]:
    return [
        "## Process overlay",
        f"- Source of truth: {profile['source_of_truth']}",
        f"- Task source of truth: {profile['task_source_of_truth']}",
        f"- Orchestrator model: {profile['orchestrator_model']}",
        f"- Routing model: {profile['routing_model']}",
        f"- Shared stages: {_shared_stage_text(profile['shared_stages'])}",
        f"- Role model: {profile['role_model']}",
        f"- TDD expectations: {profile['tdd_expectations']}",
        f"- Verification discipline: {profile['verification_discipline']}",
        f"- Acceptance flow: {profile['acceptance_flow']}",
        f"- Commit and recovery: {profile['commit_recovery']}",
    ]


def _render_project_overlay(profile: dict[str, Any]) -> list[str]:
    return [
        "## Project overlay",
        f"- {profile['summary']}",
        *profile["workspace_overlay"],
    ]


def _render_scaffold_sections(profile: dict[str, Any]) -> list[str]:
    return [
        "## Init scaffold",
        *profile["init_scaffold"],
        "",
        "## Prompt scaffold",
        *profile["prompt_scaffold"],
        "",
    ]


def _render_stage_prompt_scaffolding(profile: dict[str, Any]) -> list[str]:
    lines = ["## Stage prompt scaffolding"]
    for stage in profile["shared_stages"]:
        stage_instructions = profile.get("stage_instructions", {}).get(stage, [])
        stage_overlay = profile.get("stage_overlay", {}).get(stage, [])
        if not stage_instructions and not stage_overlay:
            continue
        lines.extend(["", f"### {stage}"])
        lines.extend(stage_instructions)
        lines.extend(stage_overlay)
    lines.append("")
    return lines


def render_context_template(profile_name: str) -> str:
    profile = resolve_process_profile(profile_name)
    lines = [
        "# Litehive Workspace Context",
        "",
        f"Process profile: {profile['label']}",
        "",
        "Describe this repository and how subagents should work in it.",
        "",
        "## Project",
        "- Purpose:",
        "- Main package/module locations:",
        "- Commands to know:",
        "",
    ]
    lines.extend(_render_process_overlay(profile))
    lines.append("")
    lines.extend(_render_project_overlay(profile))
    lines.append("")
    lines.extend(_render_scaffold_sections(profile))
    lines.extend(_render_stage_prompt_scaffolding(profile))
    if profile.get("specifics_heading"):
        lines.append(profile["specifics_heading"])
        lines.extend(profile.get("specifics", []))
        lines.append("")
    lines.extend(
        [
            "## Development rules",
            *profile["development_rules"],
            "",
            "## Tool usage",
            *profile["tool_usage"],
            "",
        ]
    )
    return "\n".join(lines)


def render_workspace_gitignore() -> str:
    return "\n".join(
        [
            "# Transient litehive runtime state",
            ".lock",
            ".runner.lock",
            "logs/",
            "pool-summary.txt",
            "engine-monitoring.yaml",
            "worktrees/",
            "tasks/*/runtime.yaml",
            "tasks/*/reports/commit_to_git-*.yaml",
            "",
        ]
    )


def ensure_workspace(root: Path, config: LitehiveConfig | None = None) -> Path:
    root = root.resolve()
    base = workspace_dir(root)
    tasks = base / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)

    cfg = config or LitehiveConfig()
    if not config_path(root).exists():
        initial_config = asdict(cfg) if config is not None else {}
        config_path(root).write_text(
            yaml.safe_dump(initial_config, sort_keys=False),
            encoding="utf-8",
        )

    if not state_path(root).exists():
        state_path(root).write_text(
            yaml.safe_dump(
                {
                    "active_task_id": None,
                    "mode": cfg.implementation_mode_name,
                    "queue": [],
                    "pool_stop_reason": None,
                    "next_task_number": 0,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    if not context_path(root).exists():
        context_path(root).write_text(
            render_context_template(cfg.process_profile), encoding="utf-8"
        )

    if not workspace_gitignore_path(root).exists():
        workspace_gitignore_path(root).write_text(
            render_workspace_gitignore(),
            encoding="utf-8",
        )

    return base


def _read_config_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return dict(data)


def _merge_config_layers(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_config_layers(
                dict(merged[key]),
                value,
            )
            continue
        merged[key] = value
    return merged


def load_effective_config_data(root: Path) -> dict[str, Any]:
    default_data = asdict(LitehiveConfig())
    global_data = _read_config_mapping(global_config_path())
    local_data = _read_config_mapping(config_path(root))
    return _merge_config_layers(
        _merge_config_layers(default_data, global_data),
        local_data,
    )


def load_config(root: Path) -> LitehiveConfig:
    ensure_workspace(root)
    data = load_effective_config_data(root)
    if data.get("process_profile") not in PROCESS_PROFILES:
        data["process_profile"] = "generic"
    if data.get("pool_selection_policy") not in VALID_POOL_SELECTION_POLICIES:
        data["pool_selection_policy"] = "dependency_aware"
    return LitehiveConfig(**data)


def load_context(root: Path) -> str:
    ensure_workspace(root)
    return context_path(root).read_text(encoding="utf-8")


def format_external_engine_sandbox(config: LitehiveConfig) -> str:
    sandbox = config.external_engine_sandbox
    if not sandbox.enabled:
        return "disabled"
    policy_parts: list[str] = []
    for engine_name in sorted(sandbox.engine_policies):
        policy = sandbox.engine_policies[engine_name]
        envs = ",".join(policy.environment) or "-"
        creds = ",".join(item.env_var for item in policy.credential_inputs) or "-"
        policy_parts.append(
            f"{engine_name}=enabled:{policy.enabled} net:{policy.network_mode or sandbox.default_network_mode} "
            f"workspace:{policy.workspace_mode or sandbox.default_workspace_mode} env:{envs} creds:{creds}"
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
        return "none"
    parts: list[str] = []
    for point in sorted(config.runner_hooks):
        hooks = ", ".join(
            f"{'blocking' if hook.blocking else 'non-blocking'}:{hook.command}"
            for hook in config.runner_hooks[point]
        )
        parts.append(f"{point}=[{hooks}]")
    return "; ".join(parts)
