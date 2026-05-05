"""Process profile rendering helpers."""

from litehive.config.profiles.loader import resolve_process_profile
from litehive.config.profiles.model import ProcessProfile


def _shared_stage_text(stages: list[str]) -> str:
    """Format the profile's stage list as a single ``a -> b -> c.`` line for the ``Shared stages`` overlay bullet; isolated so the renderer's main flow stays as a list of bullet strings."""
    return " -> ".join(stages) + "."


def _render_process_overlay(profile: ProcessProfile) -> list[str]:
    """Build the ``## Process overlay`` markdown block listing the profile's source-of-truth, models, stages, and discipline knobs; one of four section renderers stitched together by :func:`render_context_template`."""
    return [
        "## Process overlay",
        f"- Source of truth: {profile.source_of_truth}",
        f"- Task source of truth: {profile.task_source_of_truth}",
        f"- Orchestrator model: {profile.orchestrator_model}",
        f"- Routing model: {profile.routing_model}",
        f"- Shared stages: {_shared_stage_text(profile.shared_stages)}",
        f"- Role model: {profile.role_model}",
        f"- TDD expectations: {profile.tdd_expectations}",
        f"- Verification discipline: {profile.verification_discipline}",
        f"- Acceptance flow: {profile.acceptance_flow}",
        f"- Commit and recovery: {profile.commit_recovery}",
    ]


def _render_project_overlay(profile: ProcessProfile) -> list[str]:
    """Build the ``## Project overlay`` markdown block (summary line plus profile-supplied workspace bullets); one of four section renderers stitched together by :func:`render_context_template`."""
    return [
        "## Project overlay",
        f"- {profile.summary}",
        *profile.workspace_overlay,
    ]


def _render_scaffold_sections(profile: ProcessProfile) -> list[str]:
    """Build the ``## Init scaffold`` and ``## Prompt scaffold`` markdown blocks back-to-back; one of four section renderers stitched together by :func:`render_context_template`."""
    return [
        "## Init scaffold",
        *profile.init_scaffold,
        "",
        "## Prompt scaffold",
        *profile.prompt_scaffold,
        "",
    ]


def _render_stage_prompt_scaffolding(profile: ProcessProfile) -> list[str]:
    """Build the ``## Stage prompt scaffolding`` markdown block, emitting one ``### <stage>`` subsection per stage that actually has instructions or overlay text; the per-stage renderer that fills out the bottom of the workspace context template."""
    lines = ["## Stage prompt scaffolding"]
    for stage in profile.shared_stages:
        stage_instructions = profile.stage_instructions.get(stage, [])
        stage_overlay = profile.stage_overlay.get(stage, [])
        if not stage_instructions and not stage_overlay:
            continue
        lines.extend(["", f"### {stage}"])
        lines.extend(stage_instructions)
        lines.extend(stage_overlay)
    lines.append("")
    return lines


def render_context_template(profile_name: str) -> str:
    """Render the full ``CONTEXT.md`` markdown template for a named process profile, stitching together the profile/project overlays, scaffolding sections, optional specifics, and rules; called by ``litehive init`` to seed the workspace's per-process docs."""
    profile = resolve_process_profile(profile_name)
    lines = [
        "# Litehive Workspace Context",
        "",
        f"Process profile: {profile.label}",
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
    if profile.specifics_heading:
        lines.append(profile.specifics_heading)
        lines.extend(profile.specifics)
        lines.append("")
    lines.extend(
        ["## Development rules", *profile.development_rules, "", "## Tool usage", *profile.tool_usage, ""]
    )
    return "\n".join(lines)
