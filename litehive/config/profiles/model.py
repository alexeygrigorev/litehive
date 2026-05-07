"""Typed model for process profile data.

A process profile is the overlay that tailors Litehive's stage prompts and
workspace scaffolding to a particular flavor of project (generic, codehive,
django, python, rust, …). Profiles are merged at load time: the
``SHARED_PROCESS_PROFILE`` defaults are layered with a per-profile overlay,
and the result is consumed by the workspace bootstrap renderer and by stage
agents that compose their prompts from layered instruction blocks.

The Pydantic model replaces the prior ``dict[str, Any]`` shape so consumers can
rely on attribute access and tighter types instead of free-form keys.
"""

from pydantic import BaseModel, ConfigDict, Field


class ProcessProfile(BaseModel):
    """
    Resolved process profile consumed by rendering and prompt assembly.

    Each field is the merged result of the shared base plus the selected
    overlay (see ``litehive.config.profiles.loader.resolve_process_profile``).
    """

    model_config = ConfigDict(frozen=True)

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

    shared_stages: list[str] = Field(default_factory=list)
    """Ordered pipeline stage names rendered as the shared stage chain."""

    prompt_scaffold: list[str] = Field(default_factory=list)
    """Bullet guidance for how stage prompts are scaffolded; rendered in the prompt-scaffold section."""

    init_scaffold: list[str] = Field(default_factory=list)
    """Bullet guidance for how ``.litehive/context.md`` is seeded; rendered in the init-scaffold section."""

    development_rules: list[str] = Field(default_factory=list)
    """Workflow rules contributors should follow; rendered in the development-rules section."""

    tool_usage: list[str] = Field(default_factory=list)
    """Profile-specific tool/command guidance; rendered in the tool-usage section."""

    workspace_overlay: list[str] = Field(default_factory=list)
    """Workspace-level guidance bullets that complement the project summary; rendered in the project overlay block."""

    specifics: list[str] = Field(default_factory=list)
    """Optional profile-specific bullets rendered under ``specifics_heading``."""

    stage_instructions: dict[str, list[str]] = Field(default_factory=dict)
    """Default per-stage instruction bullets indexed by pipeline stage name; consumed by stage agents and rendering."""

    stage_overlay: dict[str, list[str]] = Field(default_factory=dict)
    """Per-profile per-stage overlay bullets appended after ``stage_instructions`` for that stage."""

    specifics_heading: str | None = None
    """Markdown heading for the ``specifics`` section, or ``None`` to skip it."""
