"""Prompt building for subagent stages."""

from pathlib import Path

from litehive.config import LitehiveConfig, default_agent_startup_guidance, resolve_process_profile
from litehive.models import TaskRecord
from litehive.tasks import (
    infer_acceptance_criteria,
    missing_acceptance_criteria_reason,
    task_template,
)


def stage_prompt(
    task: TaskRecord,
    step: str,
    workspace_context: str = "",
    *,
    process_profile: str = "generic",
    role_name: str | None = None,
    config: LitehiveConfig | None = None,
    root: Path | None = None,
) -> str:
    """Build the prompt for a stage subagent."""
    profile = resolve_process_profile(process_profile)
    workspace_overlay = profile.get("workspace_overlay", [])
    stage_overlay = profile.get("stage_overlay", {}).get(step, [])
    stage_instructions = profile.get("stage_instructions", {}).get(
        step, ["Complete the requested stage."]
    )
    lifecycle_verification_overlay: list[str] = []
    stage_owner = role_name or _stage_owner_for_step(step)
    stage_role = _stage_role_prompt(step, stage_owner)
    startup_guidance = _agent_startup_guidance(config, stage_owner, root=root)

    lines = [
        f"Task: {task.id} {task.title}",
        f"Stage: {step}",
        f"Stage owner: {stage_owner}",
        f"Process profile: {profile['label']}",
        f"Task type: {task.task_type or '-'}",
        "",
        "Workspace context:",
        workspace_context.strip() or "No workspace context provided.",
        "",
        "Shared process:",
        f"- Orchestrator model: {profile['orchestrator_model']}",
        f"- Routing model: {profile['routing_model']}",
        f"- Shared stages: {' -> '.join(profile['shared_stages'])}.",
        f"- Role model: {profile['role_model']}",
        f"- Source of truth: {profile['source_of_truth']}",
        f"- Task source of truth: {profile['task_source_of_truth']}",
        f"- TDD expectations: {profile['tdd_expectations']}",
        f"- Verification discipline: {profile['verification_discipline']}",
        f"- Acceptance flow: {profile['acceptance_flow']}",
        f"- Commit and recovery: {profile['commit_recovery']}",
        "",
        "Project overlay:",
        f"- {profile['summary']}",
    ]
    lines.extend(workspace_overlay or ["- No project-specific overlay provided."])
    lines.extend(
        [
            "",
            "Prompt scaffold:",
            *profile.get("prompt_scaffold", []),
            "",
            "Role focus:",
            *stage_role,
        ]
    )
    if startup_guidance:
        lines.extend(
            [
                "",
                "Project startup guidance:",
                *startup_guidance,
            ]
        )
    lines.extend(
        [
            "",
            "Stage instructions:",
            *stage_instructions,
        ]
    )
    lines.extend(stage_overlay)
    lines.extend(lifecycle_verification_overlay)
    lines.extend(
        [
            "",
            "Goal:",
            task.goal or task.title,
            "",
            "Acceptance criteria:",
        ]
    )
    if task.acceptance_criteria:
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
    else:
        lines.append("- No acceptance criteria defined.")
    missing_criteria_reason = missing_acceptance_criteria_reason(task)
    if missing_criteria_reason is not None:
        inferred_acceptance_criteria = infer_acceptance_criteria(task)
        lines.extend(["", "Acceptance gate:"])
        if step == "grooming" and inferred_acceptance_criteria:
            lines.extend(
                [
                    "- Structured acceptance criteria are still missing on the task record, but the current task context is sufficient to infer them.",
                    "- As the planner for grooming, either provide explicit `ACCEPTANCE_CRITERIA:` bullets or let the runner persist the inferred version by returning `VERDICT: PASS`.",
                    "- You may return `VERDICT: PASS` without restating them; the runner will infer and persist the criteria after grooming.",
                    "- If the current task context is not sufficient after all, return `VERDICT: BLOCKED` instead of passing grooming without criteria.",
                    "- To override the inferred version, you may add an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets that can be persisted directly.",
                    "- Return `VERDICT: BLOCKED` only if the inferred criteria are incomplete or incorrect and the task still needs more information.",
                    "",
                    "Inferred acceptance criteria available from current task context:",
                ]
            )
            lines.extend(f"- {item}" for item in inferred_acceptance_criteria)
        else:
            lines.extend(
                [
                    f"- {missing_criteria_reason}",
                    "- Use grooming or task intake to define the missing criteria before implementation starts.",
                ]
            )
            if step == "grooming":
                lines.extend(
                    [
                        "- As the planner for grooming, provide an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets before passing grooming.",
                        "- If the context is still insufficient, return `VERDICT: BLOCKED` and explain the missing information in `SUMMARY` or `WARNINGS`.",
                    ]
                )

    if step == "grooming":
        lines.extend(
            [
                "",
                "Acceptance criteria best practices:",
                "- Write each criterion as an observable outcome the user or reviewer can verify, not an implementation step.",
                "- A good criterion answers: what should happen or be true when this task is done?",
                '- Bad: "Add a validation function" — describes code to write, not behavior to verify.',
                '- Good: "Submitting invalid input shows a clear error message" — describes observable behavior.',
                '- Bad: "Refactor the module" — vague, no verifiable outcome.',
                '- Good: "The module exports the same public API and all existing tests still pass" — specific and testable.',
                '- Bad: "Run `npm test`" — just a command, not an expectation.',
                '- Good: "`npm test` passes with zero failures" — ties the command to a verifiable result.',
                "- Each criterion should be independently checkable without reading the implementation.",
                "- Avoid listing commands alone; always pair them with the expected outcome.",
            ]
        )

    template = task_template(task)
    if template is not None:
        lines.extend(
            [
                "",
                "Task template:",
                f"- Use the `{task.task_type}` template to keep the task structured.",
            ]
        )
        prompt_guidance = template.get("prompt_guidance", [])
        if isinstance(prompt_guidance, list):
            lines.extend(f"- {item}" for item in prompt_guidance)
        brief_sections = template.get("brief_sections", [])
        if isinstance(brief_sections, list):
            lines.extend(["", "Template sections to fill or verify:"])
            lines.extend(f"- {item}" for item in brief_sections)

    handoff = task.runtime.continuation_handoff
    if handoff is not None and handoff.step == step:
        lines.extend(["", "Continuation handoff:"])
        lines.append(f"- Kind: {handoff.kind}")
        lines.append(f"- Reason: {handoff.reason}")
        if handoff.from_engine or handoff.to_engine:
            lines.append(
                f"- Engine path: {handoff.from_engine or '-'} -> {handoff.to_engine or handoff.from_engine or '-'}"
            )
        if handoff.from_model or handoff.to_model:
            lines.append(
                f"- Model path: {handoff.from_model or '-'} -> {handoff.to_model or handoff.from_model or '-'}"
            )
        if handoff.attempt is not None:
            lines.append(f"- Prior attempt: {handoff.attempt}")
        if handoff.subagent_id or handoff.subagent_path:
            lines.append(
                f"- Prior subagent: {handoff.subagent_id or '-'} at `{handoff.subagent_path or '-'}`"
            )
        if handoff.summary:
            lines.append(f"- Prior summary: {handoff.summary}")
        if handoff.transcript_snippet:
            lines.append(f"- Prior snippet: {handoff.transcript_snippet}")
        if handoff.continuation is not None and handoff.continuation.resume_id:
            lines.append(f"- Engine resume id: {handoff.continuation.resume_id}")
        artifact_parts = [
            path
            for path in (
                handoff.session_path,
                handoff.report_path,
                handoff.transcript_path,
            )
            if path
        ]
        if artifact_parts:
            lines.append(
                f"- Handoff artifacts: {', '.join(f'`{path}`' for path in artifact_parts)}"
            )
        if handoff.warnings:
            lines.extend(["- Prior warnings:"] + [f"  - {warning}" for warning in handoff.warnings])
        lines.extend(
            [
                "- Continue from the prior stage context instead of restarting discovery from scratch.",
                "- Reuse the recorded artifacts and continuation identifiers when they help you preserve progress safely.",
            ]
        )

    lines.extend(["", "Plan:"])
    if task.plan:
        lines.extend(f"- {item}" for item in task.plan)
    else:
        lines.append("- No plan defined.")

    lines.extend(["", "PM sizing:"])
    lines.append(f"- Current PM complexity: {task.pm_complexity or '-'}")
    lines.append(f"- Current planned effort: {task.planned_effort or '-'}")
    if step == "grooming":
        lines.extend(
            [
                "- During grooming, set PM sizing when you have enough context.",
                "- Use `PM_COMPLEXITY: simple|moderate|complex`.",
                "- Use `PLANNED_EFFORT: xs|s|m|l|xl`.",
            ]
        )

    lines.extend(["", "Constraints:"])
    if task.constraints:
        lines.extend(f"- {item}" for item in task.constraints)
    else:
        lines.append("- Keep changes scoped to the task.")

    lines.append("")
    if step == "grooming" and missing_criteria_reason is not None:
        lines.extend(
            [
                "ACCEPTANCE_CRITERIA:",
                "- optional criterion",
            ]
        )

    # Include the task discussion thread so agents see the full history
    from litehive.tasks import render_task_thread

    thread_text = render_task_thread(root, task) if root is not None else ""
    if thread_text:
        lines.extend(["", thread_text])

    # Instruct agents to submit their verdict via CLI
    lines.extend(
        [
            "",
            "IMPORTANT: When you are done, you MUST submit your verdict by running:",
            f'  litehive report --task-id $LITEHIVE_TASK_ID --verdict <pass|fail|reject|blocked> --role {stage_owner} --step {step} --message "<your report>"',
            f"The environment variable LITEHIVE_TASK_ID is set to {task.id} for this session. Always use --task-id $LITEHIVE_TASK_ID to ensure the verdict goes to the correct task.",
            "",
            "Your --message is the PRIMARY way the next agent understands what happened.",
            "Do NOT rely on your raw transcript being read — write the report as if it is the only thing the next agent will see.",
            "",
            "Report requirements:",
            "- On PASS/ACCEPT: explain what you verified, what tests you ran, what evidence confirms the acceptance criteria are met.",
            "- On REJECT: you MUST include ALL of the following:",
            "  1. EXPECTED behavior: what should happen according to the acceptance criteria",
            "  2. OBSERVED behavior: what actually happens (exact error messages, test output, wrong values)",
            "  3. Steps to reproduce: the exact command or test that demonstrates the gap",
            "  4. Which acceptance criteria are not met and which ones are already satisfied",
            "- On FAIL: explain what went wrong and whether it is fixable or needs a different approach.",
            "- On BLOCKED: explain what dependency or resource is missing.",
            "",
            "A vague rejection like 'tests fail' or 'missing evidence' is useless and causes infinite loops.",
            "A good rejection looks like: 'Expected: `litehive engine gemini` switches the default engine and prints confirmation. "
            "Observed: command exits 0 but config.yaml still shows the old engine. Reproduce: run `litehive engine gemini` then `cat .litehive/config.yaml`. "
            "Criteria 1-3 are met, criterion 4 (persistence) is not.'",
            "",
        ]
    )

    return "\n".join(lines)


def _stage_owner_for_step(step: str) -> str:
    return {
        "grooming": "planner",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "reviewer",
        "commit_to_git": "runner",
    }.get(step, "swe")


def _stage_role_prompt(step: str, owner: str | None = None) -> list[str]:
    owner = owner or _stage_owner_for_step(step)
    if owner == "recovery":
        return [
            "- You are the recovery agent responsible for diagnosing why this task stopped making progress and restoring a runnable path.",
            "- Your job is to diagnose why the previous agent failed and restore a runnable path by fixing Litehive infrastructure bugs.",
            "- Start from the failed subagent evidence first: stdout, stderr, transcript, session metadata, exit code, and any `litehive report` attempt or error.",
            "- Your job is not to redo the failed stage's work, not to re-run the task's implementation or verification, and not to submit the failed stage verdict on the previous agent's behalf.",
            "- Make the smallest effective fix needed so the task can resume the current stage and finish cleanly.",
            "- If this workspace is not already the Litehive repo, switch into the repo at `litehive_source_path` and repair Litehive there.",
            "- Work in the Litehive source repo so you can fix the orchestrator, adapters, prompts, report wiring, resume logic, or other infrastructure bugs with the smallest safe change.",
            "- run `uv run pytest` in the Litehive repo before reporting success when you changed Litehive code; keep verification targeted.",
            "- If the evidence points to a project/task bug rather than a Litehive bug, do not implement the task; report that no Litehive infrastructure fix was found and leave the task for the normal stage owner.",
            "- Submit your own recovery verdict describing the root cause, the Litehive fix you made, and why the failed stage should be retried.",
        ]
    if step == "grooming":
        return [
            "- You are the planner, a PM-style role representing the user's and product's point of view.",
            "- Frame the real user problem, clarify scope, sharpen acceptance criteria, decompose the work, identify follow-up tasks, and estimate PM sizing.",
            "- Treat the Litehive CLI as the source of truth for task shaping: use the task record fields directly, and when documenting operator guidance prefer concrete `litehive add`, `litehive update`, and `litehive intake` flows over vague prose.",
            "- Do not pass grooming with a blank task record; make sure the task has a clear goal and explicit acceptance criteria, or return a blocked outcome that names what is missing.",
            "- During grooming, you can emit a structured `TASK_UPDATE:` YAML block to update any task field (goal, acceptance_criteria, constraints, plan, pm_complexity, planned_effort, priority, auto_commit, etc.).",
            "- To close a task as duplicate, wont_do, or deferred, include `outcome: <status>` and optional `outcome_reason: <text>` in the TASK_UPDATE block.",
            "- To park a task (pause without closing), include `action: park` in the TASK_UPDATE block.",
            "- To requeue a previously parked or closed task for another pass, include `action: requeue`. To abandon it entirely, include `action: abandon`.",
            "- Do not implement code in this stage.",
            "- Scope contamination: if the task mixes in work that belongs to a separate concern, use `litehive add` to create follow-up tasks and narrow the current task via TASK_UPDATE.",
        ]
    if step == "accepting":
        return [
            "- You are the reviewer, a PM-style role representing the user's and product's point of view.",
            "- Validate the strict end-user outcome, look for regressions or missing evidence, and make a final done versus not-done judgment.",
            "- Reject work that is incomplete, weakly verified, or misaligned with the promised outcome.",
            "- If SWE shows the requested work was already implemented before this run and provides concrete verification evidence, accept the task to normal `done` rather than inventing a special closed status.",
            "- Use `wont_do`, `duplicate`, or `deferred` only when the task is genuinely obsolete, superseded, or duplicated.",
            "- You may close a task as duplicate, wont_do, or deferred by including `outcome: <status>` with optional `outcome_reason` in a TASK_UPDATE block. You may park a task with `action: park`.",
        ]
    if step == "implementing":
        return [
            "- You are the SWE responsible for completing the implementation within scope.",
            "- Start from the task record, latest report, and latest rejection or recovery artifact before broad repository exploration.",
            "- Treat the task goal, acceptance criteria, and plan as the execution contract; if they are missing or contradictory, route the issue back through grooming or recovery instead of guessing.",
            "- If the requested behavior is already implemented, do not exit silently: run the relevant verification, confirm the acceptance criteria against the existing code, and submit `litehive report --verdict pass` with explicit evidence of what you verified.",
            "- Never exit the stage without calling `litehive report`.",
            "- If the task needs scope correction rather than code changes, use `litehive update` to narrow scope or adjust the acceptance criteria so the task re-enters the pipeline with the corrected contract.",
            "- If the task is genuinely obsolete or duplicated, use `litehive close --outcome wont_do` or `litehive close --outcome duplicate` with a concrete reason instead of exiting silently.",
        ]
    if step == "testing":
        return ["- You are the QA verifier responsible for focused independent validation."]
    return ["- Follow the stage instructions and keep the report concise and explicit."]


def _load_agent_md(root: Path | None, role: str) -> list[str] | None:
    """Read .litehive/agents/{role}.md and return content as '- ' prefixed lines, or None."""
    if root is None:
        return None
    md_path = root / ".litehive" / "agents" / f"{role}.md"
    if not md_path.is_file():
        return None
    text = md_path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return [f"- {line}" for line in text.splitlines()]


def _agent_startup_guidance(
    config: LitehiveConfig | None,
    stage_owner: str,
    *,
    root: Path | None = None,
) -> list[str]:
    lines: list[str] = []
    guidance = default_agent_startup_guidance()
    if config is not None:
        for key, values in config.agent_startup_guidance.items():
            guidance.setdefault(key, []).extend(values)
    for key in ("all", stage_owner):
        md_lines = _load_agent_md(root, key)
        if md_lines is not None:
            lines.extend(md_lines)
        else:
            for item in guidance.get(key, []):
                lines.append(f"- {item}")
    return lines


def intake_prompt(brain_dump: str) -> str:
    """Build a prompt to analyze a freeform brain dump and suggest a task title and goal."""
    profile = resolve_process_profile("codehive")
    specifics = "\n".join(str(item) for item in profile.get("specifics", []))
    return f"""You are the planner for a local multi-agent coding workspace.
You are handling freeform task intake for a Codehive-style workflow.

Codehive-style specifics:
{specifics}

Analyze the following freeform specification or brain dump and turn it into a rough queued task description.
Produce only a concise title and a short goal statement that preserve the user's intent.
Do not add acceptance criteria, implementation plans, decomposition, or detailed structure.
Keep the scope high-level and reviewable so planner grooming can refine it later.
Treat the original dump as the authoritative source of detail.

Return your suggestion in exactly this format:

TITLE: <concise rough task title>
GOAL: <1-3 sentence high-level goal statement>

--- BRAIN DUMP ---
{brain_dump.strip()}
"""
