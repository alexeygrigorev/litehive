"""Task templates and brief rendering."""

from pathlib import Path

from litehive.models import TaskRecord

from .paths import task_dir


TASK_TEMPLATES: dict[str, dict[str, object]] = {
    "adapter": {
        "goal": "Define the adapter change clearly and land the required integration behavior.",
        "acceptance_criteria": [
            "The adapter entrypoint, inputs, and expected outputs are defined for the target integration.",
            "Configuration, invocation, and failure handling are updated consistently at the adapter boundary.",
            "Focused verification covers the adapter path with a representative task run or test.",
        ],
        "constraints": [
            "Keep provider-specific behavior isolated to the adapter boundary.",
            "Preserve deterministic workspace state and execution flow.",
        ],
        "plan": [
            "Inspect the existing adapter interface, config wiring, and invocation flow.",
            "Implement the adapter change close to the integration seam.",
            "Verify the adapter path with a focused test or representative run.",
        ],
        "prompt_guidance": [
            "State the target adapter seam, external dependency, and expected contract up front.",
            "Call out config, invocation, and failure-path changes explicitly.",
            "Prefer verification that exercises the adapter boundary rather than unrelated paths.",
        ],
        "brief_sections": [
            "Adapter surface: identify the entrypoint, inputs, outputs, and external system involved.",
            "Config and execution path: note which settings, command wiring, or failure handling must change.",
            "Verification evidence: capture the focused run or test that proves the adapter path works.",
        ],
        "brief_section_stubs": [
            {
                "title": "Adapter Surface",
                "prompt": "Identify the entrypoint, inputs, outputs, and external system involved.",
            },
            {
                "title": "Config and Execution Path",
                "prompt": "Note which settings, command wiring, or failure handling must change.",
            },
            {
                "title": "Verification Evidence",
                "prompt": "Capture the focused run or test that proves the adapter path works.",
            },
        ],
    },
    "bugfix": {
        "goal": "Identify the failing behavior, implement the fix, and lock it down with focused verification.",
        "acceptance_criteria": [
            "The bug or regression is described clearly enough to verify before and after behavior.",
            "The fix addresses the root cause in the affected path without broad unrelated changes.",
            "A regression test or focused verification demonstrates the issue is resolved.",
        ],
        "constraints": [
            "Prefer the smallest change that removes the failure mode.",
            "Call out any remaining edge cases or follow-up risk explicitly.",
        ],
        "plan": [
            "Reproduce or localize the failing behavior.",
            "Implement the minimal targeted fix.",
            "Run focused regression coverage for the affected behavior.",
        ],
        "prompt_guidance": [
            "Describe the broken behavior, trigger, and expected correct behavior before changing code.",
            "Aim at root cause, not just the visible symptom.",
            "Include regression coverage or equivalent focused proof that the failure is gone.",
        ],
        "brief_sections": [
            "Bug and reproduction: describe the failing behavior, trigger, and expected result.",
            "Root cause: note the suspected or confirmed cause in the affected path.",
            "Regression coverage: record the exact test or check that prevents recurrence.",
        ],
        "brief_section_stubs": [
            {
                "title": "Bug and Reproduction",
                "prompt": "Describe the failing behavior, trigger, and expected result.",
            },
            {
                "title": "Root Cause",
                "prompt": "Note the suspected or confirmed cause in the affected path.",
            },
            {
                "title": "Regression Coverage",
                "prompt": "Record the exact test or check that prevents recurrence.",
            },
        ],
    },
    "research": {
        "goal": "Answer the open question with concrete evidence and a recommendation for next action.",
        "acceptance_criteria": [
            "The research question, scope, and decision to inform are stated clearly.",
            "Findings are grounded in repository evidence, experiments, or direct inspection.",
            "The output includes a recommendation, tradeoffs, and any follow-up tasks.",
        ],
        "constraints": [
            "Prefer evidence from the repository and local experiments over speculation.",
            "Keep conclusions explicit about confidence and remaining unknowns.",
        ],
        "plan": [
            "Define the exact question and scope of the investigation.",
            "Gather evidence from code, configs, tests, or focused experiments.",
            "Summarize findings, recommendation, and concrete follow-up actions.",
        ],
        "prompt_guidance": [
            "Frame the question, scope, and decision this research should inform.",
            "Separate observed evidence from inference.",
            "End with a recommendation, tradeoffs, and concrete follow-up tasks.",
        ],
        "brief_sections": [
            "Question and scope: define what is being investigated and what is out of scope.",
            "Evidence: capture repository findings, experiments, or comparisons that support the answer.",
            "Recommendation: state the proposed next action, tradeoffs, and remaining unknowns.",
        ],
        "brief_section_stubs": [
            {
                "title": "Question and Scope",
                "prompt": "Define what is being investigated and what is out of scope.",
            },
            {
                "title": "Evidence",
                "prompt": "Capture repository findings, experiments, or comparisons that support the answer.",
            },
            {
                "title": "Recommendation",
                "prompt": "State the proposed next action, tradeoffs, and remaining unknowns.",
            },
        ],
    },
    "review": {
        "goal": "Review the target change critically and produce an actionable decision with supporting evidence.",
        "acceptance_criteria": [
            "Findings are prioritized by severity and tied to concrete files or behaviors.",
            "Open questions, assumptions, and residual risks are captured explicitly.",
            "The review result makes the next action clear: accept, revise, or investigate further.",
        ],
        "constraints": [
            "Focus on correctness, regressions, and missing verification before style nits.",
            "Keep findings concrete enough that another engineer can act on them directly.",
        ],
        "plan": [
            "Inspect the relevant change or workflow surface.",
            "Identify actionable findings and supporting evidence.",
            "Summarize the decision, open questions, and required follow-up.",
        ],
        "prompt_guidance": [
            "Prioritize correctness, regressions, and missing verification over style observations.",
            "Tie each finding to a concrete file, behavior, or risk.",
            "Make the decision explicit: accept, revise, or investigate further.",
        ],
        "brief_sections": [
            "Review scope: identify the change, workflow, or files under review.",
            "Findings: record actionable issues with severity and supporting evidence.",
            "Decision: capture accept versus revise plus open questions or residual risks.",
        ],
        "brief_section_stubs": [
            {
                "title": "Review Scope",
                "prompt": "Identify the change, workflow, or files under review.",
            },
            {
                "title": "Findings",
                "prompt": "Record actionable issues with severity and supporting evidence.",
            },
            {
                "title": "Decision",
                "prompt": "Capture accept versus revise plus open questions or residual risks.",
            },
        ],
    },
    "intake": {
        "goal": "Capture a brain dump or freeform specification and prepare it for further decomposition.",
        "acceptance_criteria": [
            "The original brain dump is preserved and accessible.",
            "The rough task title and goal accurately reflect the high-level intent.",
            "The task is queued in 'tasks' mode for further grooming.",
        ],
        "constraints": [
            "Do not try to fully scope or structure the work at intake time.",
            "Preserve the original dump as the authoritative source of intent.",
        ],
        "plan": [
            "Review the brain dump for high-level intent.",
            "Extract a concise title and clear goal statement.",
            "Prepare the task for planner grooming.",
        ],
        "prompt_guidance": [
            "Keep the scope high-level; the planner will handle decomposition later.",
            "Ensure the original intent is preserved and linked to the task.",
        ],
        "brief_sections": [
            "Intake Notes: capture the core brain dump or link to the source.",
            "Intent summary: describe the high-level goal in a few sentences.",
        ],
        "brief_section_stubs": [
            {
                "title": "Intake Notes",
                "prompt": "Capture the core brain dump or link to the source.",
            },
            {
                "title": "Intent Summary",
                "prompt": "Describe the high-level goal in a few sentences.",
            },
        ],
    },
    "refactor": {
        "goal": "Improve the structure of the targeted area while preserving existing behavior.",
        "acceptance_criteria": [
            "The targeted code path is simpler, clearer, or better factored after the change.",
            "Behavior remains unchanged for the intended surface area.",
            "Focused verification demonstrates no regression in the refactored path.",
        ],
        "constraints": [
            "Avoid broad opportunistic cleanup outside the chosen seam.",
            "Preserve existing behavior unless the task explicitly includes functional changes.",
        ],
        "plan": [
            "Identify the narrow seam to refactor and the behavior that must stay stable.",
            "Restructure the code in small, reviewable steps.",
            "Run focused verification to confirm behavior is preserved.",
        ],
        "prompt_guidance": [
            "Name the seam being refactored and the behavior that must not change.",
            "Keep the scope structural unless the task explicitly includes functional change.",
            "Use focused verification to prove behavior stayed stable.",
        ],
        "brief_sections": [
            "Refactor seam: identify the module, function, or flow being reshaped.",
            "Behavior to preserve: list the user-visible or contract-level behavior that must stay the same.",
            "Verification: capture the checks that confirm the refactor did not regress behavior.",
        ],
        "brief_section_stubs": [
            {
                "title": "Refactor Seam",
                "prompt": "Identify the module, function, or flow being reshaped.",
            },
            {
                "title": "Behavior to Preserve",
                "prompt": "List the user-visible or contract-level behavior that must stay the same.",
            },
            {
                "title": "Verification",
                "prompt": "Capture the checks that confirm the refactor did not regress behavior.",
            },
        ],
    },
}


def apply_task_template_defaults(task: TaskRecord) -> TaskRecord:
    if task.mode != "tasks" or task.task_type is None:
        return task

    template = TASK_TEMPLATES.get(task.task_type)
    if template is None:
        return task

    if not task.goal.strip():
        task.goal = str(template["goal"])
    if not task.acceptance_criteria:
        task.acceptance_criteria = list(template["acceptance_criteria"])  # type: ignore[arg-type]
    if not task.constraints:
        task.constraints = list(template["constraints"])  # type: ignore[arg-type]
    if not task.plan:
        task.plan = list(template["plan"])  # type: ignore[arg-type]
    return task


def task_template(task: TaskRecord) -> dict[str, object] | None:
    if task.mode != "tasks" or task.task_type is None:
        return None
    template = TASK_TEMPLATES.get(task.task_type)
    if template is None:
        return None
    return template


def task_brief_file(root: Path, task: TaskRecord) -> Path:
    return task_dir(root, task) / "brief.md"


def _template_list(template: dict[str, object], key: str) -> list[str]:
    value = template.get(key, [])
    return list(value) if isinstance(value, list) else []


def _template_section_stubs(template: dict[str, object]) -> list[dict[str, str]]:
    value = template.get("brief_section_stubs", [])
    if not isinstance(value, list):
        return []

    stubs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        prompt = str(item.get("prompt", "")).strip()
        if not title or not prompt:
            continue
        stubs.append({"title": title, "prompt": prompt})
    return stubs


def render_task_brief(task: TaskRecord) -> str:
    lines = [
        f"# {task.id} {task.title}",
        "",
        f"- Mode: {task.mode}",
        f"- Task type: {task.task_type or '-'}",
        f"- PM complexity: {task.pm_complexity or '-'}",
        f"- Planned effort: {task.planned_effort or '-'}",
        "",
        "## Goal",
        task.goal or task.title,
        "",
        "## Acceptance Criteria",
    ]
    if task.acceptance_criteria:
        lines.extend(f"- {item}" for item in task.acceptance_criteria)
    else:
        lines.append("- No acceptance criteria defined.")

    lines.extend(["", "## Constraints"])
    if task.constraints:
        lines.extend(f"- {item}" for item in task.constraints)
    else:
        lines.append("- Keep changes scoped to the task.")

    lines.extend(["", "## Plan"])
    if task.plan:
        lines.extend(f"- {item}" for item in task.plan)
    else:
        lines.append("- No plan defined.")

    lines.extend(["", "## PM Sizing"])
    lines.append(f"- Complexity: {task.pm_complexity or 'Not estimated.'}")
    lines.append(f"- Planned effort: {task.planned_effort or 'Not sized.'}")

    template = task_template(task)
    if template is not None:
        lines.extend(["", "## Template Guidance"])
        lines.extend(f"- {item}" for item in _template_list(template, "prompt_guidance"))
        lines.extend(["", "## Intake Notes"])
        section_stubs = _template_section_stubs(template)
        if section_stubs:
            for stub in section_stubs:
                lines.extend(
                    [
                        "",
                        f"### {stub['title']}",
                        f"- {stub['prompt']}",
                        "",
                        "_TBD_",
                    ]
                )
        else:
            lines.extend(f"- {item}" for item in _template_list(template, "brief_sections"))

    return "\n".join(lines) + "\n"
