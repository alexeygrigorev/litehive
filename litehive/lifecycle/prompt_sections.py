"""Section builders for ``serialize_prompt``.

Each ``_*_section`` function renders one labeled block of the engine-facing
prompt. They are kept separate from the orchestrator in
``prompt_serializer.py`` so the orchestrator file stays readable and so
section-level changes don't churn the activity-handling code.

Small formatting helpers (``_compact_list``, ``_string_list``,
``_label_to_heading``, ``_human_stage_label``, ``_compact_failure_signal``,
``_single_line``) live here too because they're only used by the section
builders.
"""

from typing import TYPE_CHECKING, Any

from litehive.domain.task import TaskRecord

if TYPE_CHECKING:
    from .prompt_types import AgentPrompt, RecoveryPrompt


def _header_section(prompt: "AgentPrompt", task_record: TaskRecord | None) -> str:
    """
    Identify the task and pipeline coordinates.

    Always rendered first so the agent's response can be attributed to
    a specific run; the retry counter only appears once the task has
    actually retried so a fresh attempt does not start with noise about
    a missing prior pass.
    """
    if task_record is not None:
        title_suffix = f" — {task_record.title}"
    else:
        title_suffix = ""
    lines = [
        f"Task: {prompt.task_id}{title_suffix}",
        f"Stage: {prompt.stage}",
        f"Role: {prompt.role}",
        f"Pipeline mode: {prompt.pipeline_mode.value}",
    ]
    retry = prompt.stage_retry or 0
    if retry:
        lines.append(f"Stage retry attempt: {retry}")
    return "\n".join(lines)


def _instructions_section(prompt: "AgentPrompt") -> str:
    """
    Render the role/profile/attempt instruction layers.

    The layer order and the md-overrides-startup precedence are owned
    by ``RoleAgent._assemble_instruction_layers``; this function only
    formats the ordered ``(label, text)`` pairs, so layer ordering
    decisions stay in one place.
    """
    layers = prompt.instruction_layers or []
    if not layers:
        return ""
    blocks: list[str] = []
    for label, text in layers:
        if not text:
            continue
        blocks.append(f"## {_label_to_heading(label)}\n{text.strip()}")
    if not blocks:
        return ""
    return "Instructions:\n\n" + "\n\n".join(blocks)


def _goal_section(task_record: TaskRecord | None) -> str:
    """State the task goal, falling back to the title when no goal is defined so the section is never empty."""
    if task_record is None:
        return "Goal:\n(task record not loaded)"
    return f"Goal:\n{task_record.goal or task_record.title}"


def _acceptance_criteria_section(task_record: TaskRecord | None) -> str:
    """Render the acceptance bullets the accepting stage will check this work against — the SWE prompt uses them as the contract for ``pass``."""
    if task_record is None or not task_record.acceptance_criteria:
        return "Acceptance criteria:\n- (none defined)"
    bullets = "\n".join(f"- {c}" for c in task_record.acceptance_criteria)
    return f"Acceptance criteria:\n{bullets}"


def _plan_section(task_record: TaskRecord | None) -> str:
    """Render the plan steps grooming committed to so later stages execute against the same plan instead of re-deriving one mid-stream."""
    if task_record is None or not task_record.plan:
        return "Plan:\n- (no plan)"
    bullets = "\n".join(f"- {step}" for step in task_record.plan)
    return f"Plan:\n{bullets}"


def _constraints_section(task_record: TaskRecord | None) -> str:
    """
    Render task-level constraints.

    Supplies a default scope reminder when none are recorded so a
    SWE working on a task with no explicit constraints still sees a
    "stay in scope" line in its prompt.
    """
    if task_record is None or not task_record.constraints:
        return "Constraints:\n- Keep changes scoped to the task."
    bullets = "\n".join(f"- {c}" for c in task_record.constraints)
    return f"Constraints:\n{bullets}"


def _last_rejection_section(rejection: dict[str, Any]) -> str:
    """Surface the prior reject verdict as first-class context so the retry agent answers it directly instead of guessing from the activity log."""
    lines = [
        "Last rejection (context from the previous attempt at this stage):",
        f"- Source: {rejection.get('source')}",
        f"- Raised at phase: {rejection.get('raised_at_phase')}",
    ]
    classification = rejection.get("classification")
    if classification:
        lines.append(f"- Classification: {classification}")
    lines.append(f"- Reason: {rejection.get('reason')}")
    return "\n".join(lines)


def _prior_work_section(last_report: dict[str, Any], last_rejection: dict[str, Any] | None = None) -> str:
    """Summarize what the previous retry actually changed so the next attempt builds on it instead of redoing it.

    Test results that quote the rejection reason are dropped to avoid echoing the reject section.
    """
    changed_files = _string_list(last_report.get("changed_files"))
    test_results = _string_list(last_report.get("test_results"))
    rejection_reason = str((last_rejection or {}).get("reason") or "").strip()
    if rejection_reason:
        test_results = [result for result in test_results if rejection_reason not in result]

    lines: list[str] = []
    if changed_files:
        lines.append(f"- Changed files: {_compact_list(changed_files, limit=4)}")
    else:
        files_changed = int(last_report.get("files_changed") or 0)
        if files_changed:
            lines.append(f"- Files changed: {files_changed}")

    if test_results:
        lines.append(f"- Test results: {_compact_list(test_results, limit=2, separator='; ')}")

    if not lines:
        return ""
    return "Prior work (last attempt):\n" + "\n".join(lines)


def _recovery_trigger_section(recovery_trigger: dict[str, Any], prompt: "RecoveryPrompt") -> str:
    """
    Tell the recovery agent which failure shape kicked the task out
    of the normal pipeline.

    Without this section the recovery agent has to reconstruct the
    trigger from the journal; surfacing it here lets the agent see the
    structured fingerprint payload it would otherwise have to dig for.
    """
    lines = ["Recovery trigger (what sent the task into recovery):"]
    for key, value in recovery_trigger.items():
        lines.append(f"- {key}: {value}")
    explanation = prompt.recovery_failure_explanation
    if explanation:
        lines.append(f"- recovery_failure_explanation: {explanation}")
    return "\n".join(lines)


def _recovery_execution_root_section(prompt: "RecoveryPrompt") -> str:
    """Point the recovery agent at the Litehive source checkout to patch, not the task worktree.

    Recovery exists to fix Litehive infrastructure bugs; without this redirect the agent edits
    the wrong tree.
    """
    lines = ["Recovery execution repo (this recovery turn should patch Litehive here):"]
    lines.append(f"- recovery_execution_root: {prompt.recovery_execution_root}")
    source_path = str(prompt.litehive_source_path or "").strip()
    if source_path:
        lines.append(f"- configured_litehive_source_path: {source_path}")
    config_diagnostic = prompt.recovery_config_diagnostic
    if isinstance(config_diagnostic, dict):
        lines.append("- config_diagnostic:")
        for key in ("kind", "config_root", "exception_type", "message"):
            value = config_diagnostic.get(key)
            if value:
                lines.append(f"  - {key}: {value}")
    lines.append(
        "- Recovery is for Litehive infrastructure bugs. Do not use the task worktree as the primary edit target."
    )
    return "\n".join(lines)


def _failed_subagent_diagnostics_section(diagnostics: dict[str, Any]) -> str:
    """
    Hand the recovery agent persisted evidence of the failed subagent
    run.

    Recovery diagnosis depends on knowing exactly what the prior agent
    produced (or failed to produce); inlining the diagnostics keeps the
    agent from having to re-walk the on-disk artifacts to reconstruct
    that picture.
    """
    exit_code_value = diagnostics.get("exit_code")
    if exit_code_value is not None:
        exit_code_label = exit_code_value
    else:
        exit_code_label = "-"
    if diagnostics.get("did_produce_output"):
        did_produce_output_label = "yes"
    else:
        did_produce_output_label = "no"
    lines = [
        "Failed subagent evidence (DB-backed recovery state):",
        f"- subagent_id: {diagnostics.get('subagent_id') or '-'}",
        f"- role: {diagnostics.get('role') or '-'}",
        f"- engine: {diagnostics.get('engine') or '-'}",
        f"- status: {diagnostics.get('status') or '-'}",
        f"- path: {diagnostics.get('path') or '-'}",
        f"- exit_code: {exit_code_label}",
        f"- did_produce_output: {did_produce_output_label}",
    ]
    session_payload = diagnostics.get("session")
    if isinstance(session_payload, dict) and session_payload:
        for key in ("created_at", "updated_at"):
            if session_payload.get(key):
                lines.append(f"- session_{key}: {session_payload[key]}")
    report_payload = diagnostics.get("report")
    if isinstance(report_payload, dict) and report_payload:
        summary = str(report_payload.get("summary") or report_payload.get("message") or "").strip()
        if summary:
            lines.append(f"- report_summary: {_single_line(summary, limit=240)}")
    signal = _compact_failure_signal(
        str(diagnostics.get("stderr") or ""),
        str(diagnostics.get("stdout") or ""),
        str(diagnostics.get("transcript") or ""),
    )
    if signal:
        lines.append(f"- output_signal: {signal}")
    lines.extend(
        [
            "- Inspect with `litehive task evidence <task_id>` first; use `litehive pipeline journal <task_id>` when routing detail is needed.",
            "- Do not rerun the failed stage's task work or submit that stage's verdict yourself.",
        ]
    )
    return "\n".join(lines)


def _compact_failure_signal(*texts: str) -> str:
    """
    Pick the first failure-flavored line from stdout/stderr/transcript.

    Used by the recovery diagnostics section to surface a one-line
    signal of *what went wrong* without forcing the operator-facing
    prompt to carry the full transcript; the keyword list is the
    minimal set that catches the failure modes we have seen in
    practice.
    """
    keywords = ("litehive agent report", "traceback", "error", "failed", "exception")
    for text in texts:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and any(keyword in stripped.lower() for keyword in keywords):
                return _single_line(stripped, limit=240)
    return ""


def _single_line(value: str, limit: int) -> str:
    """Collapse whitespace and truncate so multi-line evidence fits one prompt bullet — used by the failure-signal helper above."""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _recovery_history_section(recovery_history: list[dict[str, Any]]) -> str:
    """
    Show prior recovery attempts on this task to the recovery agent.

    Without this, the agent cannot tell that it is on its second or
    third pass at the same failure and may re-try the same fix instead
    of escalating; surfacing the history is what enables the
    "spot the loop and stop" behaviour the role guidance requires.
    """
    lines = ["Recovery history (persisted prior recovery attempts for this task):"]
    recent = recovery_history[-5:]
    if len(recovery_history) > len(recent):
        lines.append(f"- Showing the latest {len(recent)} of {len(recovery_history)} recovery attempts.")
    for item in recent:
        lines.append(
            "- "
            + " ".join(
                part
                for part in (
                    f"created_at={item.get('created_at') or '-'}",
                    f"origin_stage={item.get('origin_stage') or '-'}",
                    f"fingerprint={item.get('fingerprint') or '-'}",
                    f"budget_key={item.get('budget_key') or '-'}",
                    f"verdict={item.get('recovery_verdict') or '-'}",
                    f"disposition={item.get('disposition') or '-'}",
                )
            )
        )
    return "\n".join(lines)


def _failed_run_history_section(failed_run_history: list[dict[str, Any]]) -> str:
    """
    Carry forward stage-failure shape counts to the agent.

    These counts persist across requeue/reset and are the only signal
    the agent has that it has hit the same failure shape on a previous
    run — without this section, a retry budget that the lifecycle is
    enforcing silently would look invisible to the agent itself.
    """
    lines = ["Failed-run history (survives requeue/reset for this task):"]
    recent = failed_run_history[-5:]
    if len(failed_run_history) > len(recent):
        lines.append(f"- Showing the latest {len(recent)} of {len(failed_run_history)} failed-run records.")
    for item in recent:
        lines.append(
            "- "
            + " ".join(
                part
                for part in (
                    f"stage={item.get('stage') or '-'}",
                    f"shape={item.get('failure_shape') or '-'}",
                    f"count={item.get('count') or 0}",
                    f"retry_limit={item.get('retry_limit') or '-'}",
                    f"latest_at={item.get('latest_at') or '-'}",
                    f"operator_override_count={item.get('operator_override_count') or 0}",
                )
            )
        )
        reason = str(item.get("last_reason") or "").strip()
        if reason:
            lines.append(f"  reason={reason}")
    return "\n".join(lines)


def _repeated_recovery_fingerprint_section(repeated_recovery_fingerprint: dict[str, Any]) -> str:
    """
    Force the recovery agent to escalate when the same failure
    fingerprint recurs.

    This block is what flips the recovery role's behaviour from
    "diagnose and resume" to "file a follow-up bug task and reject" —
    without it, the agent would keep re-routing the task through the
    same failing path forever.
    """
    lines = [
        "Repeated recovery fingerprint detected:",
        f"- count_including_current: {repeated_recovery_fingerprint.get('count')}",
        f"- origin_stage: {repeated_recovery_fingerprint.get('origin_stage') or '-'}",
        f"- fingerprint: {repeated_recovery_fingerprint.get('fingerprint') or '-'}",
        f"- budget_key: {repeated_recovery_fingerprint.get('budget_key') or '-'}",
        "- Escalation required: do not resume or advance again for this same failure path.",
        "- Create a follow-up bug task for the failure, then reject and include `--follow-up-task <task-id>` in the recovery report.",
    ]
    return "\n".join(lines)


def _scope_analysis_section(scope_analysis: dict[str, Any]) -> str:
    """
    Tell the recovery agent whether deleted files look like scope creep
    or legitimate operator cleanup.

    Without this signal, the recovery agent treats every unexpected
    deletion as suspect and produces noisy false-positive rejects;
    the analysis lets it distinguish "operator pruned a stale module"
    from "SWE deleted code outside its scope".
    """
    lines = ["Scope analysis (operator cleanup vs SWE scope creep):"]

    is_operator_cleanup = scope_analysis.get("is_operator_cleanup", False)
    if is_operator_cleanup:
        classification = "OPERATOR CLEANUP"
    else:
        classification = "POTENTIAL SCOPE CREEP"
    lines.append(f"- Classification: {classification}")

    reasoning = scope_analysis.get("reasoning", "No analysis available")
    lines.append(f"- Reasoning: {reasoning}")

    deleted_files = scope_analysis.get("deleted_files", [])
    if deleted_files:
        lines.append(f"- Deleted files ({len(deleted_files)}): {', '.join(deleted_files)}")

        broken_on_main = scope_analysis.get("broken_on_main", [])
        if broken_on_main:
            lines.append(f"- Files broken on main ({len(broken_on_main)}): {', '.join(broken_on_main)}")

        healthy_on_main = scope_analysis.get("healthy_on_main", [])
        if healthy_on_main:
            lines.append(f"- Files healthy on main ({len(healthy_on_main)}): {', '.join(healthy_on_main)}")
    else:
        lines.append("- No files deleted")

    return "\n".join(lines)


def _test_failure_attribution_section(attribution: dict[str, Any]) -> str:
    """
    Tell the recovery agent whether failing tests touch this task's
    surface or look pre-existing.

    Stops the recovery agent from blaming the SWE for unrelated
    breakage — when the failing tests do not touch any file the SWE
    changed, the failure is almost certainly a flake or a pre-existing
    breakage on main rather than something the SWE introduced.
    """
    classification = str(attribution.get("classification") or "unknown").replace("_", " ").upper()
    lines = ["Test failure attribution (current recovery trigger):", f"- Classification: {classification}"]
    reasoning = str(attribution.get("reasoning") or "").strip()
    if reasoning:
        lines.append(f"- Reasoning: {reasoning}")
    failing_tests = _string_list(attribution.get("failing_tests"))
    if failing_tests:
        lines.append(f"- Failing tests: {_compact_list(failing_tests, limit=2, separator='; ')}")
    matched_changed_files = _string_list(attribution.get("matched_changed_files"))
    if matched_changed_files:
        lines.append(f"- Matched changed files: {_compact_list(matched_changed_files, limit=3)}")
    changed_files = _string_list(attribution.get("changed_files"))
    if changed_files:
        lines.append(f"- Recorded changed surface: {_compact_list(changed_files, limit=4)}")
    return "\n".join(lines)


def _merge_conflict_section(conflict_files: list[str], merge_attempt: int | None) -> str:
    """
    List the files the merge stage couldn't auto-resolve.

    Rendered into the merge-resolver agent's prompt so it knows which
    files to fix before re-merging; the merge attempt counter rides
    along so the agent can see whether this is a fresh conflict or a
    retry after a previous resolution failure.
    """
    bullets = "\n".join(f"- {f}" for f in conflict_files)
    if merge_attempt is not None:
        extra = f"\nMerge attempt: {merge_attempt}"
    else:
        extra = ""
    return f"Merge conflict files (resolve all of these):\n{bullets}{extra}"


def _nudge_section(prompt: "AgentPrompt") -> str:
    """
    Re-prompt the agent to actually submit a verdict.

    Rendered when AgentNode catches ``NudgeRequired`` and rebuilds the
    prompt with ``nudge=True``; the explicit "you didn't submit"
    framing is what turns a normal turn into a verdict-submission
    nudge so the agent doesn't drift back into exploratory work.
    """
    message = str(prompt.nudge_message or "").strip()
    lines = [
        "IMPORTANT: this is a nudge because your prior turn ended without a verdict submission.",
    ]
    if message:
        lines.append(message)
    lines.append(
        "Do not continue exploratory work until you have submitted `litehive agent report` with the correct verdict."
    )
    return "\n".join(lines)


def _string_list(value: Any) -> list[str]:
    """
    Coerce report payload values into a deduplicated string list.

    Tolerates the loose typing of subagent JSON — entries can be
    strings, numbers, or empty — so a malformed report does not abort
    prompt rendering halfway through.
    """
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _compact_list(items: list[str], limit: int, separator: str = ", ") -> str:
    """Render a long list as ``a, b, c, +N more`` so prior-work bullets stay one prompt line instead of blowing past the prompt budget."""
    if len(items) <= limit:
        return separator.join(items)
    shown = separator.join(items[:limit])
    return f"{shown}{separator}+{len(items) - limit} more"


def _runner_hooks_section(stage: str | None, hooks: list[dict[str, Any]]) -> str:
    """
    Warn the agent which post-stage hooks will gate the verdict.

    Showing the hook commands (and descriptions) here lets the agent
    pre-run them locally before submitting, which dramatically reduces
    the rate of hook-rejected verdicts that would otherwise trigger a
    retry cycle.
    """
    stage_label = _human_stage_label(stage)
    lines = [f"After {stage_label}, these checks will run:"]
    for hook in hooks:
        cmd = hook.get("command", "")
        desc = hook.get("description", "")
        if desc:
            lines.append(f"- {cmd} ({desc})")
        else:
            lines.append(f"- {cmd}")
    lines.append("Hook failures can reject the stage. Run these checks yourself before submitting your verdict.")
    return "\n".join(lines)


def _verdict_instructions_section(prompt: "AgentPrompt") -> str:
    """
    Tell the agent the exact CLI invocation and verdict vocabulary for
    its role.

    Always rendered last so the agent reads the verdict instructions
    immediately before deciding what to submit. The role determines
    the allowed verdict set — recovery has a wider menu (resume,
    advance, done, budget_hit, reject) than the regular pipeline
    roles (pass, reject).
    """
    if prompt.role == "recovery":
        verdicts = "<resume|advance|done|budget_hit|reject>"
    else:
        verdicts = "<pass|reject>"
    if prompt.role == "recovery":
        example_verdict = "resume"
    else:
        example_verdict = "pass"
    return (
        "IMPORTANT: when you are done, submit your verdict by running:\n"
        f'  litehive agent report --verdict {example_verdict} --message "your report text"\n'
        f"Allowed verdicts for your role: {verdicts}.\n\n"
        "If the message is multiline or contains shell-sensitive characters, write it to /tmp/verdict_msg.txt and pass --message-file /tmp/verdict_msg.txt instead.\n"
        "Your message is the primary signal the next agent receives — write it as if it's the only thing they read.\n"
        "On reject: include EXPECTED behavior, OBSERVED behavior, reproduction steps, and which acceptance criteria are not met."
    )


def _label_to_heading(label: str) -> str:
    """
    Map prompt-builder layer keys to the human heading the agent reads.

    The instruction-section renderer uses raw keys like ``role``,
    ``attempt:fresh``, ``swe:md``; this lookup turns them into
    operator-readable headings so the prompt does not leak internal
    layer naming into the agent context.
    """
    mapping = {
        "role": "Role guidance",
        "attempt:fresh": "Fresh attempt guidance",
        "attempt:retry": "Retry attempt guidance",
        "all:startup": "Cross-role startup guidance",
        "all:md": "Cross-role workspace overlay",
        "profile": "Process profile",
    }
    if label in mapping:
        return mapping[label]
    if label.endswith(":startup"):
        return f"{label.split(':', 1)[0].title()} startup guidance"
    if label.endswith(":md"):
        return f"{label.split(':', 1)[0].title()} workspace overlay"
    return label.title()


def _human_stage_label(stage: str | None) -> str:
    """De-snake stage names for the runner-hooks heading, falling back to a generic label when no stage is set so the heading still parses."""
    if not stage:
        return "this stage"
    return stage.replace("_", " ")
