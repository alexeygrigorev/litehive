"""Prompt building for subagent stages."""

from pathlib import Path

from litehive.config.model import LitehiveConfig
from litehive.roles.guidance import default_startup_guidance
from litehive.config.profiles.loader import resolve_process_profile
from litehive.domain.task import TaskRecord
from litehive.tasks.normalization import (
    infer_acceptance_criteria,
    missing_acceptance_criteria_reason,
)


_REJECTING_HOOK_POINTS: dict[str, str] = {
    "implementing": "after_implementing",
    "testing": "after_testing",
}


def stage_prompt(
    task: TaskRecord,
    stage: str,
    workspace_context: str = "",
    *,
    process_profile: str = "generic",
    role_name: str | None = None,
    config: LitehiveConfig | None = None,
    root: Path | None = None,
) -> str:
    """Build the prompt for a stage subagent."""
    single_mode = getattr(task, "pipeline_mode", "full") == "single"
    profile = resolve_process_profile(process_profile)
    workspace_overlay = profile.get("workspace_overlay", [])
    stage_overlay = profile.get("stage_overlay", {}).get(stage, [])
    stage_instructions = profile.get("stage_instructions", {}).get(stage, ["Complete the requested stage."])
    lifecycle_verification_overlay: list[str] = []
    stage_owner = role_name or _stage_owner_for_stage(stage)
    stage_role = _stage_role_prompt(stage, stage_owner)
    startup_guidance = _agent_startup_guidance(config, stage_owner, root=root)

    lines = [
        f"Task: {task.id} {task.title}",
        f"Stage: {stage}",
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
    hook_summary = _runner_hook_prompt_lines(stage, config)
    if hook_summary:
        lines.extend(
            [
                "",
                "Checks that will reject your work if they fail:",
                *hook_summary,
                "- IMPORTANT: Run these checks yourself before finishing. If they fail, your work will be rejected.",
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
    if single_mode and stage == "implementing":
        lifecycle_verification_overlay.extend(
            [
                "- Pipeline mode is `single`: you are the only execution-stage agent for this task.",
                "- Own the full task outcome yourself: use the goal and acceptance criteria as the contract, do the necessary implementation or research/docs/config work, and verify the result directly.",
                "- Do not assume separate `testing` or `accepting` stages will clean this up later; your report must stand on its own.",
                "- If you change files and the task passes, the runner may continue to `commit_to_git`. If you do not change files and the task passes, the task can finish at `done` immediately.",
            ]
        )
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
        if stage == "grooming" and inferred_acceptance_criteria:
            lines.extend(
                [
                    "- Structured acceptance criteria are still missing on the task record, but the current task context is sufficient to infer them.",
                    '- As the planner for grooming, either provide explicit `ACCEPTANCE_CRITERIA:` bullets or let the runner persist the inferred version after you submit `litehive report --verdict pass --role planner --message "criteria confirmed"`.',
                    "- You may submit that pass verdict without restating the inferred criteria; the runner will persist them after grooming.",
                    '- If the current task context is not sufficient after all, submit `litehive report --verdict reject --role planner --message "missing context"` instead of passing grooming without criteria.',
                    "- To override the inferred version, you may add an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets that can be persisted directly.",
                    "- If the inferred criteria are incomplete or incorrect and the task still needs more information, do not pass grooming; explain the gap clearly in your report.",
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
            if stage == "grooming":
                lines.extend(
                    [
                        "- As the planner for grooming, provide an `ACCEPTANCE_CRITERIA:` section with concrete `- ` bullets before passing grooming.",
                        '- If the context is still insufficient, explain the missing information in your report and submit `litehive report --verdict reject --role planner --message "missing context"`.',
                    ]
                )

    if stage == "grooming":
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

    handoff = task.runtime.continuation_handoff
    if handoff is not None and handoff.stage == stage:
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
            lines.append(f"- Prior subagent: {handoff.subagent_id or '-'} at `{handoff.subagent_path or '-'}`")
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
            lines.append(f"- Handoff artifacts: {', '.join(f'`{path}`' for path in artifact_parts)}")
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

    lines.extend(["", "Constraints:"])
    if task.constraints:
        lines.extend(f"- {item}" for item in task.constraints)
    else:
        lines.append("- Keep changes scoped to the task.")

    lines.append("")
    if stage == "grooming" and missing_criteria_reason is not None:
        lines.extend(
            [
                "ACCEPTANCE_CRITERIA:",
                "- optional criterion",
            ]
        )

    # Include the task discussion thread so agents see the full history
    from litehive.tasks.reports import render_task_thread

    thread_text = render_task_thread(root, task, for_prompt=True) if root is not None else ""
    if thread_text:
        lines.extend(["", thread_text])

    # Instruct agents to submit their verdict via CLI
    verdicts = "<resume|advance|done|budget_hit|reject>" if stage_owner == "recovery" else "<pass|reject>"
    example_verdict = "resume" if stage_owner == "recovery" else "pass"
    lines.extend(
        [
            "",
            "IMPORTANT: When you are done, you MUST submit your verdict by running:",
            f'  litehive report --verdict {example_verdict} --role {stage_owner} --message "your report text"',
            f"Your allowed verdicts are {verdicts}.",
            f"The environment variable LITEHIVE_TASK_ID is set to {task.id} for this session. Workspace and task resolution should use the injected environment automatically.",
            "",
            "If the message is multiline or contains shell-sensitive characters, you may write it to `/tmp/verdict_msg.txt` and use `--message-file /tmp/verdict_msg.txt` instead.",
            "",
            "Your report is the PRIMARY way the next agent understands what happened.",
            "Do NOT rely on your raw transcript being read — write the report as if it is the only thing the next agent will see.",
            "",
            "Report requirements:",
            "- On PASS: explain what you verified, what tests you ran, what evidence confirms the acceptance criteria are met.",
            "- On REJECT: you MUST include ALL of the following:",
            "  1. EXPECTED behavior: what should happen according to the acceptance criteria",
            "  2. OBSERVED behavior: what actually happens (exact error messages, test output, wrong values)",
            "  3. Steps to reproduce: the exact command or test that demonstrates the gap",
            "  4. Which acceptance criteria are not met and which ones are already satisfied",
            "",
            "A vague rejection like 'tests fail' or 'missing evidence' is useless and causes infinite loops.",
            "A good rejection looks like: 'Expected: `litehive engine gemini` switches the default engine and prints confirmation. "
            "Observed: command exits 0 but config.yaml still shows the old engine. Reproduce: run `litehive engine gemini` then `cat .litehive/config.yaml`. "
            "Criteria 1-3 are met, criterion 4 (persistence) is not.'",
            "",
        ]
    )

    return "\n".join(lines)


def _stage_owner_for_stage(stage: str) -> str:
    return {
        "grooming": "planner",
        "implementing": "swe",
        "testing": "qa",
        "accepting": "reviewer",
        "commit_to_git": "runner",
    }.get(stage, "swe")


def _runner_hook_prompt_lines(stage: str, config: LitehiveConfig | None) -> list[str]:
    if config is None:
        return []
    hook_point = _REJECTING_HOOK_POINTS.get(stage)
    if hook_point is None:
        return []
    hooks = config.runner_hooks.get(hook_point, [])
    rejecting = [h for h in hooks if h.reject_on_failure]
    if not rejecting:
        return []
    lines: list[str] = []
    for hook in rejecting:
        lines.append(f"- {_format_runner_hook_prompt_entry(hook)}")
    return lines


def _format_runner_hook_prompt_entry(hook: object) -> str:
    description = getattr(hook, "description", None)
    command = getattr(hook, "command")
    if description:
        return f"{command} ({description})"
    return command


def _stage_role_prompt(stage: str, owner: str | None = None) -> list[str]:
    owner = owner or _stage_owner_for_stage(stage)
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
    if stage == "grooming":
        return [
            "- You are the planner, a PM-style role representing the user's and product's point of view.",
            "- Frame the real user problem, clarify scope, sharpen acceptance criteria, decompose the work, identify follow-up tasks.",
            "- Treat the Litehive CLI as the source of truth for task shaping. Use explicit CLI commands to mutate task state:",
            "  - `litehive agent update --goal ... --acceptance-criteria ... --plan-step ...` to rewrite task fields.",
            "  - `litehive task add ...` to create follow-up tasks when the current task mixes concerns.",
            "  - `litehive task close <task-id> --outcome duplicate|wont_do|deferred --reason ...` to close.",
            "- Do not pass grooming with a blank task record; make sure the task has a clear goal and explicit acceptance criteria, or reject it with a clear explanation of what is missing.",
            "- Do not implement code in this stage.",
        ]
    if stage == "accepting":
        return [
            "- You are the reviewer, a PM-style role representing the user's and product's point of view.",
            "- Validate the strict end-user outcome, look for regressions or missing evidence, and make a final done versus not-done judgment.",
            "- Reject work that is incomplete, weakly verified, or misaligned with the promised outcome.",
            "- If SWE shows the requested work was already implemented before this run and provides concrete verification evidence, accept the task to normal `done` rather than inventing a special closed status.",
            "- Use `wont_do`, `duplicate`, or `deferred` only when the task is genuinely obsolete, superseded, or duplicated.",
            "- You may close a task as duplicate, wont_do, or deferred via `litehive task close <task-id> --outcome <status> --reason <text>`.",
        ]
    if stage == "implementing":
        return [
            "- You are the SWE responsible for completing the implementation within scope.",
            "- Start from the task record, latest report, and latest rejection or recovery artifact before broad repository exploration.",
            "- Treat the task goal, acceptance criteria, and plan as the execution contract; if they are missing or contradictory, route the issue back through grooming or recovery instead of guessing.",
            "- Before assuming the work is already implemented, run `git diff main...HEAD` in your worktree.",
            "- If there are no changes, implement from scratch regardless of what prior stage reports claim.",
            '- Only skip implementation and submit `litehive report --verdict pass --role swe --message "already implemented and verified"` if `git diff main...HEAD` shows the expected changes and the acceptance criteria are met.',
            "- Never exit the stage without calling `litehive report`.",
            "- If the task needs scope correction rather than code changes, use `litehive task update` to narrow scope or adjust the acceptance criteria so the task re-enters the pipeline with the corrected contract.",
            "- If the task is genuinely obsolete or duplicated, use `litehive task close --outcome wont_do` or `litehive task close --outcome duplicate` with a concrete reason instead of exiting silently.",
        ]
    if stage == "testing":
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
    guidance = default_startup_guidance()
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
