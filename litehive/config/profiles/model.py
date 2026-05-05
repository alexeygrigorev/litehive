"""Typed model for process profile data.

A process profile is the overlay that tailors Litehive's stage prompts and
workspace scaffolding to a particular flavor of project (generic, codehive,
django, python, rust, …). Profiles are merged at load time: the
``SHARED_PROCESS_PROFILE`` defaults are layered with a per-profile overlay,
and the result is consumed by the workspace bootstrap renderer and by stage
agents that compose their prompts from layered instruction blocks.

The dataclass replaces the prior ``dict[str, Any]`` shape so consumers can
rely on attribute access and tighter types instead of free-form keys.
"""

from dataclasses import dataclass
from typing import Any


def _copy_stage_bullet_map(raw: dict[str, Any]) -> dict[str, list[str]]:
    """
    Shallow-copy a profile stage→bullets mapping for the dataclass.

    Wraps each bullet list in ``list(...)`` so the loader's merged
    structure cannot be mutated through the resolved
    :class:`ProcessProfile` (the dataclass is frozen, but its
    list values would otherwise still be aliased). Caller:
    :meth:`ProcessProfile.from_dict`.
    """
    copied: dict[str, list[str]] = {}
    for stage, bullets in raw.items():
        copied[stage] = list(bullets)
    return copied


@dataclass(frozen=True)
class ProcessProfile:
    """Resolved process profile consumed by rendering and prompt assembly.

    Each field is the merged result of the shared base plus the selected
    overlay (see ``litehive.config.profiles.loader.resolve_process_profile``).
    """

    label: str
    """Human-readable profile name shown in the workspace context header."""

    summary: str
    """One-line description of the workflow flavor; used in the project overlay block."""

    source_of_truth: str
    """Where authoritative task and implementation state lives; rendered in the process overlay."""

    task_source_of_truth: str
    """Which records define task scope (issues, queued tasks, …); rendered in the process overlay."""

    orchestrator_model: str
    """How the local runner relates to subagents (manager vs. peer); rendered in the process overlay."""

    routing_model: str
    """How routing, retries, and escalation are decided; rendered in the process overlay."""

    role_model: str
    """Mapping of roles (planner/reviewer/swe/qa) to stage ownership; rendered in the process overlay."""

    tdd_expectations: str
    """Test-first / regression-first expectations for SWE work; rendered in the process overlay."""

    verification_discipline: str
    """How QA verification should evidence regressions; rendered in the process overlay."""

    acceptance_flow: str
    """What must hold before the reviewer can accept; rendered in the process overlay."""

    commit_recovery: str
    """Commit checkpoint and recover policy summary; rendered in the process overlay."""

    shared_stages: list[str]
    """Ordered pipeline stage names rendered as the shared stage chain."""

    prompt_scaffold: list[str]
    """Bullet guidance for how stage prompts are scaffolded; rendered in the prompt-scaffold section."""

    init_scaffold: list[str]
    """Bullet guidance for how ``.litehive/context.md`` is seeded; rendered in the init-scaffold section."""

    development_rules: list[str]
    """Workflow rules contributors should follow; rendered in the development-rules section."""

    tool_usage: list[str]
    """Profile-specific tool/command guidance; rendered in the tool-usage section."""

    workspace_overlay: list[str]
    """Workspace-level guidance bullets that complement the project summary; rendered in the project overlay block."""

    specifics: list[str]
    """Optional profile-specific bullets rendered under ``specifics_heading``."""

    stage_instructions: dict[str, list[str]]
    """Default per-stage instruction bullets indexed by pipeline stage name; consumed by stage agents and rendering."""

    stage_overlay: dict[str, list[str]]
    """Per-profile per-stage overlay bullets appended after ``stage_instructions`` for that stage."""

    specifics_heading: str | None = None
    """Markdown heading for the ``specifics`` section, or ``None`` to skip it."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProcessProfile":
        """Build a ``ProcessProfile`` from a fully-merged profile dict.

        Used by the loader after it merges the shared defaults with the
        selected overlay. Lists and dicts are taken as-is — the loader has
        already performed deep copies during merge.
        """
        return cls(
            label=data["label"],
            summary=data["summary"],
            source_of_truth=data["source_of_truth"],
            task_source_of_truth=data["task_source_of_truth"],
            orchestrator_model=data["orchestrator_model"],
            routing_model=data["routing_model"],
            role_model=data["role_model"],
            tdd_expectations=data["tdd_expectations"],
            verification_discipline=data["verification_discipline"],
            acceptance_flow=data["acceptance_flow"],
            commit_recovery=data["commit_recovery"],
            shared_stages=list(data["shared_stages"]),
            prompt_scaffold=list(data["prompt_scaffold"]),
            init_scaffold=list(data["init_scaffold"]),
            development_rules=list(data["development_rules"]),
            tool_usage=list(data["tool_usage"]),
            workspace_overlay=list(data["workspace_overlay"]),
            specifics=list(data.get("specifics", [])),
            stage_instructions=_copy_stage_bullet_map(data.get("stage_instructions", {})),
            stage_overlay=_copy_stage_bullet_map(data.get("stage_overlay", {})),
            specifics_heading=data.get("specifics_heading"),
        )
